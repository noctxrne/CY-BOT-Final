from flask import Flask, render_template, request, jsonify, session, send_from_directory, url_for
import os
import uuid
import datetime
from werkzeug.utils import secure_filename
from bot_backend import get_bot_response, process_pdf, remove_pdf_from_memory
import sqlite3
from contextlib import closing

app = Flask(__name__)
app.secret_key = "cyberlaw_secret"

# Configure upload folder
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
DATABASE = 'chat_sessions.db'

# Create upload directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- DATABASE HELPER FUNCTIONS ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# def init_db():
#     if not os.path.exists(DATABASE):
#         with closing(get_db_connection()) as db:
#             with app.open_resource('schema.sql', mode='r') as f:
#                 db.cursor().executescript(f.read())
#             db.commit()

# Add this import at the top
import tempfile

# Update this section in app.py
def init_db():
    if not os.path.exists(DATABASE):
        # For GCP, use a writable temporary directory
        db_path = os.path.join(tempfile.gettempdir(), 'chat_sessions.db')
        with closing(get_db_connection(db_path)) as db:
            with app.open_resource('schema.sql', mode='r') as f:
                db.cursor().executescript(f.read())
            db.commit()

# --- ROUTES ---
@app.route("/")
def index():
    # Create a new session if not exists
    if 'session_id' not in session:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        with closing(get_db_connection()) as db:
            db.execute('INSERT INTO sessions (id, title) VALUES (?, ?)', (session_id, 'New Chat'))
            db.commit()
    
    with closing(get_db_connection()) as db:
        sessions = db.execute('SELECT * FROM sessions ORDER BY created_at DESC').fetchall()
    
    return render_template("index.html", 
                          current_session=session['session_id'],
                          sessions=sessions)

@app.route("/new_chat", methods=["POST"])
def new_chat():
    session_id = str(uuid.uuid4())
    session['session_id'] = session_id
    with closing(get_db_connection()) as db:
        db.execute('INSERT INTO sessions (id, title) VALUES (?, ?)', (session_id, 'New Chat'))
        db.commit()
    
    return jsonify({"session_id": session_id})

@app.route("/switch_chat", methods=["POST"])
def switch_chat():
    session_id = request.json["session_id"]
    with closing(get_db_connection()) as db:
        session_exists = db.execute('SELECT id FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session_exists:
            session['session_id'] = session_id
            history = db.execute('SELECT user_message, bot_message FROM messages WHERE session_id = ? ORDER BY timestamp', (session_id,)).fetchall()
            return jsonify({"success": True, "history": [dict(row) for row in history]})
    return jsonify({"success": False})

@app.route("/get_response", methods=["POST"])
def get_response():
    user_msg = request.json["message"]
    has_pdf = request.json.get("has_pdf", False)
    session_id = request.json.get("session_id", session.get('session_id'))
    
    bot_msg = get_bot_response(user_msg, has_pdf)

    with closing(get_db_connection()) as db:
        db.execute(
            'INSERT INTO messages (session_id, user_message, bot_message) VALUES (?, ?, ?)',
            (session_id, user_msg, bot_msg)
        )
        # Update title if this is the first message
        message_count = db.execute('SELECT COUNT(*) as count FROM messages WHERE session_id = ?', (session_id,)).fetchone()['count']
        if message_count == 1:
            db.execute('UPDATE sessions SET title = ? WHERE id = ?', (user_msg[:30] + ("..." if len(user_msg) > 30 else ""), session_id))
        db.commit()

    return jsonify({"bot": bot_msg})

@app.route("/clear", methods=["POST"])
def clear_chat():
    session_id = request.json.get("session_id", session.get('session_id'))
    with closing(get_db_connection()) as db:
        db.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
        db.commit()
    return jsonify({"status": "cleared"})

@app.route("/delete_chat", methods=["POST"])
def delete_chat():
    session_id = request.json["session_id"]
    with closing(get_db_connection()) as db:
        db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        db.commit()
    
    if session.get('session_id') == session_id:
        # Create a new session if the current one was deleted
        new_session_id = str(uuid.uuid4())
        session['session_id'] = new_session_id
        with closing(get_db_connection()) as db:
            db.execute('INSERT INTO sessions (id, title) VALUES (?, ?)', (new_session_id, 'New Chat'))
            db.commit()
        
    return jsonify({"success": True, "current_session": session.get('session_id')})

@app.route("/get_history", methods=["GET"])
def get_history():
    session_id = request.args.get("session_id", session.get('session_id'))
    with closing(get_db_connection()) as db:
        history = db.execute('SELECT user_message, bot_message FROM messages WHERE session_id = ? ORDER BY timestamp', (session_id,)).fetchall()
        return jsonify({"history": [dict(row) for row in history]})

@app.route("/get_sessions", methods=["GET"])
def get_sessions():
    with closing(get_db_connection()) as db:
        sessions = db.execute('SELECT * FROM sessions ORDER BY created_at DESC').fetchall()
        return jsonify({"sessions": [dict(row) for row in sessions], "current": session.get('session_id')})

# --- PDF ROUTES (no changes needed here) ---
@app.route("/upload_pdf", methods=["POST"])
def upload_pdf():
    if 'pdf' not in request.files:
        return jsonify({"success": False, "error": "No file part"})
    
    file = request.files['pdf']
    if file.filename == '':
        return jsonify({"success": False, "error": "No selected file"})
    
    if file and allowed_file(file.filename):
        pdf_id = str(uuid.uuid4())
        filename = secure_filename(f"{pdf_id}.pdf")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        
        try:
            process_pdf(pdf_id, file_path)
            pdf_url = f"/uploads/{filename}"
            return jsonify({
                "success": True, 
                "pdf_id": pdf_id,
                "pdf_url": pdf_url
            })
        except Exception as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            return jsonify({"success": False, "error": str(e)})
    
    return jsonify({"success": False, "error": "Invalid file type"})

@app.route("/remove_pdf", methods=["POST"])
def remove_pdf():
    pdf_id = request.json.get("pdf_id")
    if not pdf_id:
        return jsonify({"success": False, "error": "No PDF ID provided"})
    
    try:
        remove_pdf_from_memory(pdf_id)
        filename = secure_filename(f"{pdf_id}.pdf")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# if __name__ == "__main__":
#     init_db()
#     app.run(debug=True)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080, debug=False)