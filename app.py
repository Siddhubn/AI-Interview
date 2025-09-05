import os
import io
import json
import sqlite3
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
from dotenv import load_dotenv
import PyPDF2
import docx
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import navy, black, red

# --- App Configuration ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
DATABASE = 'interview_app.db'
REPORT_FOLDER = 'reports'
os.makedirs(REPORT_FOLDER, exist_ok=True)

# --- Database Setup ---
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def create_tables():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            FOREIGN KEY (admin_id) REFERENCES admins (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            resume_text TEXT NOT NULL,
            report_path TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs (id)
        )
    ''')
    conn.commit()
    conn.close()

with app.app_context():
    create_tables()

# --- Gemini API Configuration ---
try:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found.")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Gemini API: {e}")
    model = None

# --- Main Routes ---
@app.route('/')
@app.route('/interview/<int:job_id>')
def index(job_id=None):
    """Renders the single-page application for all routes."""
    return render_template('index.html')

# --- Admin Authentication ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    company_name = data.get('company_name')
    email = data.get('email')
    phone = data.get('phone')
    password = data.get('password')

    if not all([company_name, email, phone, password]):
        return jsonify({'error': 'All fields are required.'}), 400

    hashed_password = generate_password_hash(password)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO admins (company_name, email, phone, password) VALUES (?, ?, ?, ?)",
            (company_name, email, phone, hashed_password)
        )
        conn.commit()
        return jsonify({'message': 'Registration successful. Please login.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email already exists.'}), 409
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    conn = get_db()
    admin = conn.execute("SELECT * FROM admins WHERE email = ?", (email,)).fetchone()
    conn.close()

    if admin and check_password_hash(admin['password'], password):
        session['admin_id'] = admin['id']
        session['company_name'] = admin['company_name']
        return jsonify({'message': 'Login successful.', 'company_name': admin['company_name']})
    
    return jsonify({'error': 'Invalid email or password.'}), 401

@app.route('/api/logout')
def logout():
    session.clear()
    return jsonify({'message': 'Logout successful.'})
    
@app.route('/api/check_session')
def check_session():
    if 'admin_id' in session:
        return jsonify({'logged_in': True, 'company_name': session.get('company_name')})
    return jsonify({'logged_in': False})

# --- Admin Dashboard Routes ---
@app.route('/api/dashboard_data')
def dashboard_data():
    if 'admin_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    admin_id = session['admin_id']
    conn = get_db()
    jobs = conn.execute("SELECT * FROM jobs WHERE admin_id = ? ORDER BY id DESC", (admin_id,)).fetchall()
    
    dashboard_data = []
    for job in jobs:
        job_dict = dict(job)
        candidates = conn.execute(
            "SELECT id, SUBSTR(resume_text, 1, 100) as resume_summary, report_path FROM candidates WHERE job_id = ?",
            (job['id'],)
        ).fetchall()
        job_dict['candidates'] = [dict(c) for c in candidates]
        dashboard_data.append(job_dict)
        
    conn.close()
    return jsonify(dashboard_data)

@app.route('/api/create_job', methods=['POST'])
def create_job():
    if 'admin_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    title = data.get('title')
    description = data.get('description')

    if not title or not description:
        return jsonify({'error': 'Job title and description are required.'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO jobs (admin_id, title, description) VALUES (?, ?, ?)",
        (session['admin_id'], title, description)
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()

    interview_link = url_for('index', job_id=job_id, _external=True)
    return jsonify({'message': 'Job created successfully.', 'interview_link': interview_link})

@app.route('/api/download_report/<int:candidate_id>')
def download_report(candidate_id):
    if 'admin_id' not in session:
        return "Unauthorized", 401
    
    conn = get_db()
    candidate = conn.execute("SELECT c.report_path FROM candidates c JOIN jobs j ON c.job_id = j.id WHERE c.id = ? AND j.admin_id = ?", (candidate_id, session['admin_id'])).fetchone()
    conn.close()

    if candidate and candidate['report_path'] and os.path.exists(candidate['report_path']):
        with open(candidate['report_path'], 'rb') as f:
            pdf_data = f.read()
        return Response(pdf_data, mimetype='application/pdf', headers={
            'Content-Disposition': f'attachment;filename=report_candidate_{candidate_id}.pdf'
        })
    return "Report not found or you do not have permission to access it.", 404

# --- Candidate Interview Flow ---
@app.route('/api/start_interview', methods=['POST'])
def start_interview():
    data = request.get_json()
    job_id = data.get('job_id')
    resume_text = data.get('resume_text')

    if not job_id or not resume_text:
        return jsonify({'error': 'Missing job ID or resume text.'}), 400
    
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return jsonify({'error': 'This interview link is invalid or has expired.'}), 404
    
    cursor = conn.cursor()
    cursor.execute("INSERT INTO candidates (job_id, resume_text) VALUES (?, ?)", (job_id, resume_text))
    candidate_id = cursor.lastrowid
    conn.commit()
    
    session['candidate_id'] = candidate_id
    
    # Generate questions based on the job description
    questions_response = generate_questions_for_job(dict(job), resume_text)
    
    # Store job requirements in session for final report generation
    session['job_requirements'] = job['description']
    
    conn.close()
    return questions_response


def generate_questions_for_job(job, candidate_skills):
    if not model: return jsonify({'error': 'AI model not configured.'}), 500
    try:
        prompt = f"""Act as an expert technical hiring manager...
        **Job Requirements:**\n{job['description']}\n
        **Candidate's Skills:**\n{candidate_skills}\n
        Provide a JSON with a key "questions" holding an array of 5 string questions."""
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(cleaned_response_text))
    except Exception as e:
        return jsonify({'error': f'Failed to generate questions: {str(e)}'}), 500

# --- Shared API Endpoints ---
@app.route('/api/extract_text', methods=['POST'])
def extract_text():
    if 'file' not in request.files: return jsonify({'error': 'No file found.'}), 400
    file = request.files['file']
    text = ""
    try:
        if file.filename.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file.read()))
            for page in pdf_reader.pages: text += page.extract_text() or ""
        elif file.filename.endswith('.docx'):
            doc = docx.Document(io.BytesIO(file.read()))
            for para in doc.paragraphs: text += para.text + '\n'
        else: return jsonify({'error': 'Unsupported file type.'}), 400
        return jsonify({'text': text})
    except Exception as e:
        return jsonify({'error': f'Error processing file: {str(e)}'}), 500

@app.route('/api/make_casual', methods=['POST'])
def make_casual_api():
    if not model: return jsonify({'error': 'AI model not configured.'}), 500
    data = request.get_json()
    question = data.get('question')
    prompt = f'Rewrite this interview question in a conversational tone: "{question}". Return JSON with key "casual_question".'
    try:
        response = model.generate_content(prompt)
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(cleaned_text))
    except Exception: return jsonify({'casual_question': question})

@app.route('/api/generate_final_report', methods=['POST'])
def generate_final_report():
    if 'candidate_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if 'job_requirements' not in session: return jsonify({'error': 'Session expired, job context lost.'}), 400
        
    try:
        data = request.get_json()
        interview_results = data.get('interview_results')
        proctoring_flags = data.get('proctoring_flags', [])
        candidate_id = session['candidate_id']
        job_requirements = session['job_requirements']

        formatted_results = ""
        for i, result in enumerate(interview_results):
            formatted_results += f"Q{i+1}: {result.get('question')}\nAns: {result.get('answer')}\nScore: {result.get('score')}/10\nFeedback: {result.get('feedback')}\n\n"

        prompt = f"""Act as a senior hiring manager...
        **Job Requirements:**\n{job_requirements}\n
        **Interview Transcript & Evaluation:**\n{formatted_results}\n
        Provide a JSON scorecard with keys: "overall_summary", "strengths", "areas_for_improvement", "final_recommendation"."""
        
        response = model.generate_content(prompt)
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        scorecard_data = json.loads(cleaned_text)
        
        # --- PDF Generation and Saving ---
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='TitleStyle', fontName='Helvetica-Bold', fontSize=24, alignment=TA_CENTER, spaceAfter=20))
        styles.add(ParagraphStyle(name='Heading1Style', fontName='Helvetica-Bold', fontSize=16, spaceBefore=12, spaceAfter=6, textColor=navy))
        styles.add(ParagraphStyle(name='BulletStyle', leftIndent=20, spaceBefore=2))
        styles.add(ParagraphStyle(name='WarningStyle', leftIndent=20, spaceBefore=2, textColor=red))

        story = []
        story.append(Paragraph("Candidate Performance Report", styles['TitleStyle']))
        story.append(Paragraph("Overall Summary", styles['Heading1Style']))
        story.append(Paragraph(scorecard_data.get('overall_summary', 'N/A'), styles['Normal']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Key Strengths", styles['Heading1Style']))
        for s in scorecard_data.get('strengths', []): story.append(Paragraph(f"• {s}", styles['BulletStyle']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Areas for Improvement", styles['Heading1Style']))
        for a in scorecard_data.get('areas_for_improvement', []): story.append(Paragraph(f"• {a}", styles['BulletStyle']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Final Recommendation", styles['Heading1Style']))
        story.append(Paragraph(f"<b>{scorecard_data.get('final_recommendation', 'N/A')}</b>", styles['Normal']))
        
        if proctoring_flags:
            story.append(Spacer(1, 12))
            story.append(HRFlowable(width="100%"))
            story.append(Paragraph("Proctoring Flags", styles['Heading1Style']))
            for flag in sorted(list(set(proctoring_flags))): story.append(Paragraph(f"• {flag}", styles['WarningStyle']))
        
        doc.build(story)
        
        report_path = os.path.join(REPORT_FOLDER, f'report_candidate_{candidate_id}.pdf')
        with open(report_path, 'wb') as f:
            f.write(buffer.getvalue())
            
        conn = get_db()
        conn.execute("UPDATE candidates SET report_path = ? WHERE id = ?", (report_path, candidate_id))
        conn.commit()
        conn.close()

        session.clear()
        
        return jsonify({'message': 'Interview submitted successfully.'})
    except Exception as e:
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

