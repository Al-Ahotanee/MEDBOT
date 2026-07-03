import os
import sys
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, Response, send_file
from flask_cors import CORS

# ==========================================
# CONFIGURATION - USE ENVIRONMENT VARIABLES FOR PRODUCTION
# ==========================================
# Set these in your Render Dashboard -> Environment Variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") 
DATABASE_URL = os.environ.get("DATABASE_URL", "") # e.g., postgresql://neondb_owner:...
# ==========================================

app = Flask(__name__)
CORS(app)

def get_db_connection():
    """Helper to get a database connection to Neon PostgreSQL."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    """Initializes the database tables and default admin."""
    if not DATABASE_URL:
        print("Skipping DB Init: No DATABASE_URL provided. (Expected during local testing without Neon)")
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Create Users Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'user'
            )
        ''')
        # Create Knowledge Base Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                source VARCHAR(50) DEFAULT 'manual'
            )
        ''')
        
        # Insert Default Admin if not exists
        cur.execute('''
            INSERT INTO users (username, password, role) 
            VALUES ('admin', 'admin123', 'admin')
            ON CONFLICT (username) DO NOTHING
        ''')
        
        # Insert some seed FAQ data if the table is empty
        cur.execute('SELECT COUNT(*) FROM knowledge_base')
        count = cur.fetchone()[0]
        if count == 0:
            seed_data = [
                ("What are the symptoms of COVID-19?", "Common symptoms include fever, dry cough, and fatigue. Other symptoms may include loss of taste or smell, nasal congestion, or sore throat. *Please consult a doctor for formal testing.*", "manual"),
                ("What is a normal blood pressure?", "A normal blood pressure level is less than 120/80 mmHg. However, 'normal' can vary depending on age and medical history.", "manual"),
                ("How can I treat a mild headache?", "For mild headaches, resting in a quiet, dark room, staying hydrated, and using over-the-counter pain relievers like paracetamol or ibuprofen can help. If it persists, see a physician.", "manual")
            ]
            cur.executemany('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)', seed_data)
        
        conn.commit()
    except Exception as e:
        print(f"Database initialization error: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# Run DB init on startup
init_db()

MEDICAL_PROMPT = """
You are MediChat, an intelligent AI Medical Diagnosis and Health Assistant. Your role is to provide general health information, explain symptoms, and offer preliminary, non-diagnostic guidance.
- Tone: Empathetic, highly professional, cautious, and scientific.
- Rule 1 (CRITICAL): You are an AI, not a licensed physician. ALWAYS include a brief disclaimer in your response advising the user to consult a healthcare professional for actual medical diagnosis or severe symptoms.
- Rule 2: If a user describes life-threatening symptoms (e.g., severe chest pain, inability to breathe, sudden numbness), instruct them to seek emergency medical help immediately.
- Format: Keep answers structured and clean using markdown (bullet points, bold text).
"""

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
        
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO users (username, password, role) VALUES (%s, %s, %s)', (username, password, 'user'))
        conn.commit()
        return jsonify({"message": "Account created successfully", "user": username, "role": "user"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Username already exists"}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT * FROM users WHERE username = %s AND password = %s', (username, password))
        user = cur.fetchone()
        if user:
            return jsonify({"message": "Login successful", "user": user['username'], "role": user['role']}), 200
            
        return jsonify({"error": "Invalid credentials"}), 401
    finally:
        cur.close()
        conn.close()

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_messages = data.get('messages', [])
    if not user_messages:
        return jsonify({"error": "No messages provided"}), 400
        
    last_user_msg = user_messages[-1]['text'].strip()
    last_user_msg_lower = last_user_msg.lower()
    
    # 1. OFFLINE CHECK: Scan PostgreSQL Database for an answer
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT question, answer FROM knowledge_base')
        kb_entries = cur.fetchall()
        for entry in kb_entries:
            db_q = entry['question'].lower().strip()
            # Simple heuristic matching
            if (db_q == last_user_msg_lower) or (len(db_q) > 10 and (db_q in last_user_msg_lower or last_user_msg_lower in db_q)):
                return jsonify({
                    "response": entry['answer'], 
                    "source": "offline"
                }), 200
    finally:
        cur.close()
        conn.close()

    # 2. GEMINI FALLBACK: Call AI
    if not GEMINI_API_KEY:
         return jsonify({"error": "Server missing Gemini API Key. Cannot answer un-cached questions."}), 500
         
    formatted_history = []
    started_with_user = False
    
    for msg in user_messages:
        role = 'user' if msg['role'] == 'user' else 'model'
        if not started_with_user and role == 'model':
            continue
        started_with_user = True
        formatted_history.append({"role": role, "parts": [{"text": msg['text']}]})
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": formatted_history,
        "systemInstruction": {"parts": [{"text": MEDICAL_PROMPT}]},
        "generationConfig": {"temperature": 0.2} # Factual, precise medical tone
    }
    
    try:
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        result = response.json()
        
        if response.status_code != 200:
            error_msg = result.get('error', {}).get('message', 'Unknown Gemini API Error')
            return jsonify({"error": f"API Error: {error_msg}"}), 500
        
        if 'candidates' in result and len(result['candidates']) > 0:
            bot_text = result['candidates'][0]['content']['parts'][0]['text']
            
            # 3. STORE RESULT: Save new Q&A to Postgres
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                cur.execute('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)', 
                          (last_user_msg, bot_text, 'gemini'))
                conn.commit()
            finally:
                cur.close()
                conn.close()
                
            return jsonify({"response": bot_text, "source": "gemini"}), 200
        else:
            return jsonify({"error": "Received empty response from AI."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/kb', methods=['GET', 'POST'])
def handle_kb():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if request.method == 'GET':
            cur.execute('SELECT * FROM knowledge_base ORDER BY id DESC')
            entries = cur.fetchall()
            return jsonify(entries), 200
            
        if request.method == 'POST':
            data = request.json
            cur.execute('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)',
                       (data['question'], data['answer'], data.get('source', 'manual')))
            conn.commit()
            return jsonify({"message": "Entry added"}), 201
    finally:
        cur.close()
        conn.close()

@app.route('/api/admin/kb/<int:kb_id>', methods=['PUT', 'DELETE'])
def manage_kb_item(kb_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if request.method == 'PUT':
            data = request.json
            cur.execute('UPDATE knowledge_base SET question=%s, answer=%s, source=%s WHERE id=%s',
                       (data['question'], data['answer'], data.get('source', 'manual'), kb_id))
            conn.commit()
            return jsonify({"message": "Entry updated"}), 200
            
        if request.method == 'DELETE':
            cur.execute('DELETE FROM knowledge_base WHERE id=%s', (kb_id,))
            conn.commit()
            return jsonify({"message": "Entry deleted"}), 200
    finally:
        cur.close()
        conn.close()

@app.route('/api/admin/export', methods=['GET'])
def export_kb():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT question, answer, source FROM knowledge_base')
        entries = cur.fetchall()
        return Response(json.dumps(entries, indent=4), mimetype='application/json',
                        headers={"Content-Disposition": "attachment;filename=medical_kb.json"})
    finally:
        cur.close()
        conn.close()

@app.route('/api/admin/import', methods=['POST'])
def import_kb():
    try:
        data = request.json
        if not isinstance(data, list):
            return jsonify({"error": "Invalid format, expected list of objects"}), 400
            
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            for item in data:
                cur.execute('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)',
                           (item.get('question', ''), item.get('answer', ''), item.get('source', 'imported')))
            conn.commit()
            return jsonify({"message": f"Successfully imported {len(data)} entries."}), 200
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/')
def serve_frontend():
    """Serves the index.html from the same directory"""
    return send_file('index.html')

if __name__ == '__main__':
    # Use PORT environment variable provided by Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
