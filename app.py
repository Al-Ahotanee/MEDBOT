import os
import sys
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

# ==========================================
# CONFIGURATION
# ==========================================
# Make sure to set these Environment Variables in your Render/Neon dashboard
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/medibot")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6K69_oQdtu0Xr3atj4-e1rBDLIl93gps2lzK3uTmLAfJA")
# ==========================================

app = Flask(__name__)
CORS(app)

def get_db():
    # RealDictCursor allows us to access columns by name, just like sqlite3.Row
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        role TEXT DEFAULT 'user'
                    )
                ''')
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS knowledge_base (
                        id SERIAL PRIMARY KEY,
                        question TEXT NOT NULL,
                        answer TEXT NOT NULL,
                        source TEXT DEFAULT 'manual'
                    )
                ''')
                cur.execute('''
                    INSERT INTO users (username, password, role)
                    VALUES ('admin', 'admin123', 'admin')
                    ON CONFLICT (username) DO NOTHING
                ''')
    except Exception as e:
        print(f"Database initialization error: {e}")

# Initialize the PostgreSQL Database on startup
init_db()

MEDIBOT_PROMPT = """
You are MediBot, an advanced AI-powered medical diagnostic assistant.
- Tone: Empathetic, professional, informative, and cautious.
- Disclaimer Protocol: ALWAYS remind the user that you are an AI, not a doctor. If symptoms indicate an emergency, strongly advise them to seek emergency medical help immediately.
- Only answer health, medicine, and biology questions. Keep formatting clean using markdown.
"""

@app.route('/')
def serve_frontend():
    # Serves the index.html file from the same directory
    return send_file('index.html')

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password: return jsonify({"error": "Required fields missing"}), 400
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO users (username, password, role) VALUES (%s, %s, %s)', (username, password, 'user'))
        return jsonify({"message": "Account created", "user": username, "role": "user"}), 201
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM users WHERE username = %s AND password = %s', (data.get('username'), data.get('password')))
            user = cur.fetchone()
    if user: 
        return jsonify({"message": "Login successful", "user": user['username'], "role": user['role']}), 200
    return jsonify({"error": "Invalid credentials"}), 401

def run_builtin_engine(data):
    """
    Offline NLP / Rule-based Diagnostic Engine
    Analyzes structured input from the frontend wizard.
    """
    symptoms = [s.lower() for s in data.get('symptoms', [])]
    severity = data.get('severity', '').lower()
    duration = data.get('duration', '').lower()
    history = [h.lower() for h in data.get('history', [])]

    if severity == 'severe' or 'chest pain' in symptoms or 'shortness of breath' in symptoms:
        return """**URGENT: MEDICAL EMERGENCY**\n\nYour symptoms (especially severe intensity or involving chest/breathing) indicate a potential medical emergency. Please visit an emergency room or call emergency services immediately.\n\n_Possible related conditions: Heart Attack, Severe Asthma, Pulmonary Embolism._"""

    if 'fever' in symptoms and ('cough' in symptoms or 'sore throat' in symptoms):
        ans = "**Diagnosis:** Viral Respiratory Infection (e.g., Flu, Common Cold, COVID-19)\n\n**Advice:** Rest, stay hydrated, and monitor your temperature. "
        ans += f"Given your symptoms have lasted '{duration}', if they worsen, consult a doctor."
        return ans

    if 'vomiting' in symptoms or 'diarrhea' in symptoms or 'nausea' in symptoms:
        ans = "**Diagnosis:** Gastroenteritis (Stomach Bug) or Food Poisoning\n\n**Advice:** Focus on hydration. Drink clear fluids in small sips. Avoid solid foods until your stomach settles."
        if 'diabetes' in history: ans += "\n*Note: Monitor your blood sugar closely as illness can disrupt glucose levels.*"
        return ans

    if 'headache' in symptoms:
        if 'fever' in symptoms:
            return "**Diagnosis:** Viral Illness or potential infection\n\n**Advice:** A headache combined with a fever warrants medical evaluation if severe. Rest and hydrate."
        return "**Diagnosis:** Tension Headache or Migraine\n\n**Advice:** Rest in a quiet, dark room. Ensure you are well hydrated and consider standard over-the-counter pain relief if suitable."

    if 'fatigue' in symptoms and 'muscle ache' in symptoms:
         return "**Diagnosis:** Viral Syndrome or Overexertion\n\n**Advice:** Prioritize rest and sleep. Take warm baths to soothe muscles. Stay hydrated."

    return "**Diagnosis:** General Malaise / Non-specific Symptoms\n\n**Advice:** Your symptoms are varied. Ensure adequate rest and fluid intake. If symptoms persist for more than a few days or worsen, consult a healthcare professional."

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    engine_type = data.get('engine', 'gemini')

    # --- BUILT-IN ENGINE PATH ---
    if engine_type == 'builtin':
        diagnosis = run_builtin_engine(data.get('builtin_data', {}))
        return jsonify({"response": diagnosis, "source": "builtin-nlp"}), 200

    # --- GEMINI AI ENGINE PATH ---
    user_messages = data.get('messages', [])
    if not user_messages: return jsonify({"error": "No messages provided"}), 400
    last_user_msg = user_messages[-1]['text'].strip()

    # 1. Local DB Cache Check
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT question, answer FROM knowledge_base')
            kb_entries = cur.fetchall()
            
    for entry in kb_entries:
        if entry['question'].lower() == last_user_msg.lower():
            return jsonify({"response": entry['answer'], "source": "offline-db"}), 200

    # 2. Gemini API Call
    if not GEMINI_API_KEY:
         return jsonify({"error": "Server missing API Key.", "fallback_available": True}), 500

    formatted_history = []
    started = False
    for msg in user_messages:
        if msg.get('type') in ['wizard', 'wizard_response']: continue
        role = 'user' if msg['role'] == 'user' else 'model'
        if not started and role == 'model': continue
        started = True
        formatted_history.append({"role": role, "parts": [{"text": msg['text']}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": formatted_history, "systemInstruction": {"parts": [{"text": MEDIBOT_PROMPT}]}, "generationConfig": {"temperature": 0.2}}

    try:
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        result = response.json()

        if response.status_code != 200:
            error_msg = result.get('error', {}).get('message', 'Unknown Gemini API Error')
            return jsonify({"error": f"AI Engine Failed: {error_msg}", "fallback_available": True}), 500

        if 'candidates' in result and len(result['candidates']) > 0:
            bot_text = result['candidates'][0]['content']['parts'][0]['text']
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)', (last_user_msg, bot_text, 'gemini'))
            return jsonify({"response": bot_text, "source": "gemini"}), 200
        else:
            return jsonify({"error": "Empty response from AI.", "fallback_available": True}), 500
    except Exception as e:
        return jsonify({"error": f"Network/System Error: {str(e)}", "fallback_available": True}), 500

@app.route('/api/admin/kb', methods=['GET', 'POST'])
def handle_kb():
    if request.method == 'GET':
        with get_db() as conn: 
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM knowledge_base ORDER BY id DESC')
                return jsonify([dict(row) for row in cur.fetchall()]), 200
    if request.method == 'POST':
        data = request.json
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO knowledge_base (question, answer, source) VALUES (%s, %s, %s)', (data['question'], data['answer'], data.get('source', 'manual')))
        return jsonify({"message": "Entry added"}), 201

@app.route('/api/admin/kb/<int:kb_id>', methods=['PUT', 'DELETE'])
def manage_kb_item(kb_id):
    if request.method == 'PUT':
        data = request.json
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE knowledge_base SET question=%s, answer=%s, source=%s WHERE id=%s', (data['question'], data['answer'], data.get('source', 'manual'), kb_id))
        return jsonify({"message": "Entry updated"}), 200
    if request.method == 'DELETE':
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM knowledge_base WHERE id=%s', (kb_id,))
        return jsonify({"message": "Entry deleted"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

