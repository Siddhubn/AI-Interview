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
from flask_mail import Mail, Message

# --- App Configuration ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
DATABASE = 'hiring_platform.db'
REPORT_FOLDER = 'reports'
os.makedirs(REPORT_FOLDER, exist_ok=True)

# --- Email Configuration ---
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ['true', 'on', '1']
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])
mail = Mail(app)

# --- Database Setup ---
def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def create_tables():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY, company_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT, password TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, FOREIGN KEY (admin_id) REFERENCES admins (id))''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
            resume_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Applied', shortlist_reason TEXT,
            report_path TEXT, interview_results TEXT,
            FOREIGN KEY (candidate_id) REFERENCES candidates (id), FOREIGN KEY (job_id) REFERENCES jobs (id)
        )
    ''')
    conn.commit()
    conn.close()

with app.app_context():
    create_tables()

# --- Gemini API Configuration ---
try:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise ValueError("GEMINI_API_KEY not found.")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"FATAL: Error configuring Gemini API: {e}")
    model = None

# ==============================================================================
# TEMPLATE RENDERING & CORE ROUTES
# ==============================================================================
@app.route('/')
def index():
    return render_template('login.html')

@app.route('/dashboard')
def admin_dashboard():
    if session.get('user_type') != 'admin': return redirect(url_for('index'))
    return render_template('admin_dashboard.html')

@app.route('/candidate/dashboard')
def candidate_dashboard():
    if session.get('user_type') != 'candidate': return redirect(url_for('index'))
    return render_template('candidate_dashboard.html')

@app.route('/interview/<int:application_id>')
def interview_page(application_id):
    conn = get_db()
    app_data = conn.execute("SELECT j.title FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id = ?", (application_id,)).fetchone()
    conn.close()
    if not app_data: return "Interview link is invalid or has expired.", 404
    return render_template('interview.html', job_title=app_data['title'], application_id=application_id)

# ==============================================================================
# AUTHENTICATION API
# ==============================================================================
@app.route('/api/register/admin', methods=['POST'])
def register_admin():
    data = request.json
    hashed_password = generate_password_hash(data['password'])
    conn = get_db()
    try:
        conn.execute("INSERT INTO admins (company_name, email, phone, password) VALUES (?, ?, ?, ?)",
                     (data['company_name'], data['email'], data['phone'], hashed_password))
        conn.commit()
        return jsonify({'message': 'Registration successful.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email already exists.'}), 409
    finally: conn.close()

@app.route('/api/login/admin', methods=['POST'])
def login_admin():
    data = request.json
    conn = get_db()
    admin = conn.execute("SELECT * FROM admins WHERE email = ?", (data['email'],)).fetchone()
    conn.close()
    if admin and check_password_hash(admin['password'], data['password']):
        session['user_type'] = 'admin'
        session['admin_id'] = admin['id']
        session['company_name'] = admin['company_name']
        return jsonify({'message': 'Login successful.', 'company_name': admin['company_name']})
    return jsonify({'error': 'Invalid credentials.'}), 401
    
@app.route('/api/register/candidate', methods=['POST'])
def register_candidate():
    data = request.json
    hashed_password = generate_password_hash(data['password'])
    conn = get_db()
    try:
        conn.execute("INSERT INTO candidates (name, email, password) VALUES (?, ?, ?)",
                     (data['name'], data['email'], hashed_password))
        conn.commit()
        return jsonify({'message': 'Registration successful.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email already exists.'}), 409
    finally: conn.close()

@app.route('/api/login/candidate', methods=['POST'])
def login_candidate():
    data = request.json
    conn = get_db()
    candidate = conn.execute("SELECT * FROM candidates WHERE email = ?", (data['email'],)).fetchone()
    conn.close()
    if candidate and check_password_hash(candidate['password'], data['password']):
        session['user_type'] = 'candidate'
        session['candidate_id'] = candidate['id']
        session['candidate_name'] = candidate['name']
        return jsonify({'message': 'Login successful.'})
    return jsonify({'error': 'Invalid credentials.'}), 401

@app.route('/api/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/api/check_session')
def check_session():
    if session.get('user_type') == 'admin':
        return jsonify({'logged_in': True, 'user_type': 'admin', 'company_name': session.get('company_name')})
    if session.get('user_type') == 'candidate':
        return jsonify({'logged_in': True, 'user_type': 'candidate', 'candidate_name': session.get('candidate_name')})
    return jsonify({'logged_in': False})

# ==============================================================================
# ADMIN API
# ==============================================================================
@app.route('/api/admin/jobs')
def get_admin_jobs():
    if session.get('user_type') != 'admin': return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    jobs = conn.execute("SELECT * FROM jobs WHERE admin_id = ? ORDER BY id DESC", (session['admin_id'],)).fetchall()
    data = []
    for job in jobs:
        job_dict = dict(job)
        apps = conn.execute("SELECT a.id, a.status, c.name, c.email, a.report_path FROM applications a JOIN candidates c ON a.candidate_id = c.id WHERE a.job_id = ?", (job['id'],)).fetchall()
        job_dict['applications'] = [dict(app) for app in apps]
        data.append(job_dict)
    conn.close()
    return jsonify(data)

@app.route('/api/admin/create_job', methods=['POST'])
def create_job():
    if session.get('user_type') != 'admin': return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    conn = get_db()
    cursor = conn.execute("INSERT INTO jobs (admin_id, title, description) VALUES (?, ?, ?)",
                          (session['admin_id'], data['title'], data['description']))
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'message': 'Job created successfully.', 'interview_link': url_for('interview_page', job_id=job_id, _external=True)})
    
@app.route('/api/admin/shortlist/<int:job_id>', methods=['POST'])
def shortlist_candidates(job_id):
    if session.get('user_type') != 'admin': return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    job = conn.execute("SELECT description FROM jobs WHERE id = ? AND admin_id = ?", (job_id, session['admin_id'])).fetchone()
    applications = conn.execute("SELECT id, resume_text FROM applications WHERE job_id = ? AND status = 'Applied'", (job_id,)).fetchall()
    
    if not job: conn.close(); return jsonify({'error': 'Job not found'}), 404
    if not applications: conn.close(); return jsonify({'message': 'No new applications to shortlist.'})

    for app in applications:
        prompt = f"""
        Analyze if the candidate's resume is a good fit for the job description.
        Provide a JSON response with two keys: "shortlisted" (boolean) and "reason" (a brief explanation).

        **Job Description:**
        {job['description']}

        **Candidate Resume:**
        {app['resume_text']}
        """
        try:
            response = model.generate_content(prompt)
            result = json.loads(response.text.strip().replace('```json', '').replace('```', ''))
            if result.get('shortlisted'):
                conn.execute("UPDATE applications SET status = 'Shortlisted', shortlist_reason = ? WHERE id = ?", (result.get('reason', ''), app['id']))
        except Exception as e:
            print(f"Error shortlisting application {app['id']}: {e}")

    conn.commit()
    conn.close()
    return jsonify({'message': f'Shortlisting complete for {len(applications)} applications.'})

@app.route('/api/admin/send_invite/<int:application_id>', methods=['POST'])
def send_invite(application_id):
    if session.get('user_type') != 'admin': return jsonify({'error': 'Unauthorized'}), 401
    if not mail: return jsonify({'error': 'Email server is not configured.'}), 500
    
    conn = get_db()
    app_data = conn.execute("SELECT c.email, j.title FROM applications a JOIN candidates c ON a.candidate_id = c.id JOIN jobs j ON a.job_id = j.id WHERE a.id = ?", (application_id,)).fetchone()
    if not app_data: conn.close(); return jsonify({'error': 'Application not found.'}), 404
    
    interview_link = url_for('interview_page', application_id=application_id, _external=True)
    subject = f"Interview Invitation for the {app_data['title']} role"
    body = f"""Dear Candidate,\n\nCongratulations! Your application for the {app_data['title']} position has been shortlisted.\nPlease use the following link to complete your AI-proctored virtual interview:\n{interview_link}\n\nBest of luck!\nThe {session['company_name']} Hiring Team"""
    try:
        msg = Message(subject, recipients=[app_data['email']], body=body)
        mail.send(msg)
        conn.execute("UPDATE applications SET status = 'Invited' WHERE id = ?", (application_id,))
        conn.commit()
        return jsonify({'message': 'Interview invitation sent.'})
    except Exception as e:
        print(f"MAIL SENDING ERROR: {e}")
        return jsonify({'error': f'Failed to send email: {e}. Check server configuration.'}), 500
    finally: conn.close()

@app.route('/api/admin/update_status/<int:application_id>', methods=['POST'])
def update_status(application_id):
    if session.get('user_type') != 'admin': return jsonify({'error': 'Unauthorized'}), 401
    
    # CORRECTED: Add a check to ensure the request has a valid JSON body
    if not request.is_json:
        return jsonify({'error': 'Invalid request: Content-Type must be application/json.'}), 415

    data = request.get_json()
    status = data.get('status')
    if status not in ['Accepted', 'Rejected']: 
        return jsonify({'error': 'Invalid status provided in request body.'}), 400
    
    conn = get_db()
    app_data = conn.execute("SELECT c.email, j.title, a.report_path FROM applications a JOIN candidates c ON a.candidate_id = c.id JOIN jobs j ON a.job_id = j.id WHERE a.id = ?", (application_id,)).fetchone()
    if not app_data: conn.close(); return jsonify({'error': 'Application not found.'}), 404

    try:
        if status == 'Accepted' and mail:
            subject = "Update on your application"
            body = f"Congratulations! We would like to invite you to our office for the next round of interviews for the {app_data['title']} role."
            msg = Message(subject, recipients=[app_data['email']], body=body)
            mail.send(msg)
        
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, application_id))
        conn.commit()
        return jsonify({'message': f'Candidate status updated to {status}.'})
    except Exception as e:
        return jsonify({'error': f'Failed to send email: {e}.'}), 500
    finally: conn.close()

@app.route('/api/download_report/<int:application_id>')
def download_report(application_id):
    if 'admin_id' not in session: return "Unauthorized", 401
    conn = get_db()
    candidate = conn.execute("SELECT a.report_path FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id = ? AND j.admin_id = ?", (application_id, session['admin_id'])).fetchone()
    conn.close()
    if candidate and candidate['report_path'] and os.path.exists(candidate['report_path']):
        return Response(open(candidate['report_path'], 'rb'), mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename=report_application_{application_id}.pdf'})
    return "Report not found.", 404

# ==============================================================================
# CANDIDATE API & SHARED HELPERS
# ==============================================================================
@app.route('/api/jobs')
def get_jobs():
    if session.get('user_type') != 'candidate': return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    jobs = conn.execute("SELECT j.id, j.title, j.description, a.company_name FROM jobs j JOIN admins a ON j.admin_id = a.id ORDER BY j.id DESC").fetchall()
    conn.close()
    return jsonify([dict(job) for job in jobs])

@app.route('/api/apply/<int:job_id>', methods=['POST'])
def apply_to_job(job_id):
    if session.get('user_type') != 'candidate': return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    conn = get_db()
    existing = conn.execute("SELECT id FROM applications WHERE candidate_id = ? AND job_id = ?", (session['candidate_id'], job_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'You have already applied to this job.'}), 409
    
    conn.execute("INSERT INTO applications (candidate_id, job_id, resume_text) VALUES (?, ?, ?)",
                 (session['candidate_id'], job_id, data['resume_text']))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Application submitted successfully.'})
    
@app.route('/api/candidate/applications')
def get_candidate_applications():
    if session.get('user_type') != 'candidate': return jsonify({'error': 'Unauthorized'}), 401
    conn = get_db()
    apps = conn.execute("""
        SELECT a.id, a.status, a.report_path, j.title, adm.company_name
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        JOIN admins adm ON j.admin_id = adm.id
        WHERE a.candidate_id = ? ORDER BY a.id DESC
    """, (session['candidate_id'],)).fetchall()
    conn.close()
    return jsonify([dict(app) for app in apps])
    
def generate_questions_for_job(job, skills):
    if not model: return {"error": "AI model not configured."}
    try:
        prompt = f"""Act as an expert technical hiring manager. Generate 5 targeted interview questions...
        **Job Requirements:**\n{job['description']}\n
        **Candidate's Skills:**\n{skills}\n
        Provide a valid JSON with a key "questions" holding an array of 5 strings."""
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(cleaned_response_text)
    except Exception as e:
        print(f"Error generating questions: {e}")
        return {"questions": ["Could you please tell me about your experience?", "What is your biggest strength?", "What is your biggest weakness?", "Why are you interested in this role?", "Where do you see yourself in 5 years?"]}

@app.route('/api/start_interview', methods=['POST'])
def start_interview():
    data = request.json
    application_id = data.get('application_id')
    conn = get_db()
    app_data = conn.execute("SELECT j.description, a.resume_text FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id = ?", (application_id,)).fetchone()
    if not app_data: 
        conn.close()
        return jsonify({'error': 'Invalid interview link.'}), 404
    
    session['application_id'] = application_id
    session['job_requirements'] = app_data['description']
    conn.close()
    
    questions_data = generate_questions_for_job({'description': app_data['description']}, app_data['resume_text'])
    return jsonify(questions_data)
    
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
    data = request.json; question = data.get('question')
    prompt = f'Rewrite this interview question in a conversational tone: "{question}". Return JSON with key "casual_question".'
    try:
        response = model.generate_content(prompt)
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(cleaned_text))
    except Exception: return jsonify({'casual_question': question})

@app.route('/api/score_answer', methods=['POST'])
def score_answer():
    if not model: return jsonify({'error': 'AI model not configured.'}), 500
    try:
        data = request.get_json()
        question = data.get('question')
        answer = data.get('answer')

        if not question or not answer:
            return jsonify({'error': 'Both question and answer are required.'}), 400

        prompt = f"""
        As an expert technical interviewer, evaluate the following answer for the given question.
        Provide a score from 0 to 10 and concise, constructive feedback.

        Question: "{question}"
        Candidate's Answer: "{answer}"

        Return a valid JSON object with two keys: "score" (an integer) and "feedback" (a string).
        """
        response = model.generate_content(prompt)
        cleaned_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(cleaned_text))
    except Exception as e:
        return jsonify({'error': f'Failed to score answer: {e}'}), 500

@app.route('/api/generate_final_report', methods=['POST'])
def generate_final_report():
    if 'application_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        interview_results = data.get('interview_results')
        proctoring_flags = data.get('proctoring_flags', [])
        application_id = session['application_id']
        job_requirements = session['job_requirements']

        formatted_results = "\n".join([f"Q: {r['question']}\nA: {r['answer']}\nScore: {r['score']}/10\nFeedback: {r['feedback']}\n" for r in interview_results])

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
            story.append(Spacer(1, 12)); story.append(HRFlowable(width="100%"))
            story.append(Paragraph("Proctoring Flags", styles['Heading1Style']))
            for flag in sorted(list(set(proctoring_flags))): story.append(Paragraph(f"• {flag}", styles['WarningStyle']))
        
        doc.build(story)
        
        report_path = os.path.join(REPORT_FOLDER, f'report_application_{application_id}.pdf')
        with open(report_path, 'wb') as f: f.write(buffer.getvalue())
            
        conn = get_db()
        conn.execute("UPDATE applications SET report_path = ?, status = 'Completed', interview_results = ? WHERE id = ?", (report_path, json.dumps(interview_results), application_id))
        conn.commit()
        conn.close()

        session.clear()
        return jsonify({'message': 'Interview submitted successfully.'})
    except Exception as e:
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

