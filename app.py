import os
import io
import json
from flask import Flask, render_template, request, jsonify, Response
import google.generativeai as genai
from dotenv import load_dotenv
import PyPDF2
import docx

# PDF Generation Imports
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import navy, black, red

# Load environment variables from a .env file
load_dotenv()

app = Flask(__name__)

# --- Google Gemini API Configuration ---
try:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in .env file.")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"Error configuring Gemini API: {e}")
    model = None

@app.route('/')
def index():
    """Renders the main page of the application."""
    return render_template('index.html')

@app.route('/extract_resume_text', methods=['POST'])
def extract_resume_text():
    """API endpoint to handle resume file upload and extract text."""
    if 'resume' not in request.files:
        return jsonify({'error': 'No resume file found.'}), 400
    
    file = request.files['resume']
    filename = file.filename
    text = ""

    try:
        if filename.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file.read()))
            for page in pdf_reader.pages:
                text += page.extract_text() or ""
        elif filename.endswith('.docx'):
            doc = docx.Document(io.BytesIO(file.read()))
            for para in doc.paragraphs:
                text += para.text + '\n'
        else:
            return jsonify({'error': 'Unsupported file type. Please upload a PDF or DOCX file.'}), 400
        
        if not text.strip():
            return jsonify({'error': 'Could not extract any text from the resume. The file might be empty or image-based.'}), 400

        return jsonify({'resume_text': text})

    except Exception as e:
        print(f"Error processing resume file: {e}")
        return jsonify({'error': f'An error occurred while processing the file: {str(e)}'}), 500


@app.route('/generate_questions', methods=['POST'])
def generate_questions():
    """API endpoint to generate interview questions."""
    if not model:
        return jsonify({'error': 'The application is not configured correctly. Please check the API key.'}), 500

    try:
        data = request.get_json()
        job_requirements = data.get('job_requirements')
        candidate_skills = data.get('candidate_skills')

        if not job_requirements or not candidate_skills:
            return jsonify({'error': 'Job Requirements and Candidate Skills are required.'}), 400

        prompt = f"""
        Act as an expert technical hiring manager. Your task is to generate 5 targeted interview questions
        based on the following job requirements and candidate's resume/skills. The questions should assess
        how well the candidate's skills align with the job's needs.

        **Job Requirements:**
        {job_requirements}

        **Candidate's Resume/Skills:**
        {candidate_skills}

        Please provide the output as a valid JSON object with a single key "questions" which holds an array of 5 string questions.
        """

        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        response_json = json.loads(cleaned_response_text)
        questions = response_json.get("questions", [])

        if not questions:
            return jsonify({'error': 'Could not generate questions from the provided text.'}), 500

        return jsonify({'questions': questions})

    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse the response from the AI model.'}), 500
    except Exception as e:
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/make_casual', methods=['POST'])
def make_casual():
    """API endpoint to rewrite a formal question in a casual tone."""
    if not model:
        return jsonify({'error': 'The application is not configured correctly.'}), 500
    
    try:
        data = request.get_json()
        question = data.get('question')
        if not question:
            return jsonify({'error': 'Question is required.'}), 400

        prompt = f"""
        Rewrite the following formal interview question in a more casual, conversational, and friendly tone, as if you were a friendly hiring manager asking it in a real conversation.
        
        Formal Question: "{question}"
        
        Return the result as a valid JSON object with a single key "casual_question".
        """
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        response_json = json.loads(cleaned_response_text)
        
        return jsonify(response_json)

    except Exception as e:
        return jsonify({'casual_question': question}) # Fallback to original question on error


@app.route('/score_answer', methods=['POST'])
def score_answer():
    """API endpoint to score a candidate's answer."""
    if not model:
        return jsonify({'error': 'The application is not configured correctly. Please check the API key.'}), 500
    
    try:
        data = request.get_json()
        question = data.get('question')
        answer = data.get('answer')

        if not question or not answer:
            return jsonify({'error': 'Both question and answer are required.'}), 400

        prompt = f"""
        As an expert technical interviewer, evaluate the following answer for the given question.
        Provide a score from 0 to 10 and concise, constructive feedback on the answer's technical accuracy and clarity.

        Question: "{question}"
        Candidate's Answer: "{answer}"

        Return the evaluation as a valid JSON object with two keys: "score" (an integer) and "feedback" (a string).
        """
        
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        response_json = json.loads(cleaned_response_text)
        
        return jsonify(response_json)

    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse the scoring response from the AI model.'}), 500
    except Exception as e:
        return jsonify({'error': f'An unexpected error occurred during scoring: {str(e)}'}), 500

@app.route('/generate_final_scorecard', methods=['POST'])
def generate_final_scorecard():
    """API endpoint to generate a final performance scorecard as a PDF."""
    if not model:
        return jsonify({'error': 'The application is not configured correctly.'}), 500
        
    try:
        data = request.get_json()
        job_requirements = data.get('job_requirements')
        interview_results = data.get('interview_results')
        proctoring_flags = data.get('proctoring_flags', [])

        if not job_requirements or not interview_results:
            return jsonify({'error': 'Job requirements and interview results are required.'}), 400

        formatted_results = ""
        for i, result in enumerate(interview_results):
            formatted_results += f"Question {i+1}: {result.get('question')}\n"
            formatted_results += f"Answer: {result.get('answer')}\n"
            formatted_results += f"Score: {result.get('score')}/10\n"
            formatted_results += f"Feedback: {result.get('feedback')}\n\n"

        prompt = f"""
        Act as a senior hiring manager providing a final evaluation for a job candidate.
        Based on the job requirements and the candidate's performance during the interview (detailed below),
        generate a comprehensive final scorecard.

        **Job Requirements:**
        {job_requirements}

        **Full Interview Transcript and Evaluation:**
        {formatted_results}

        Your task is to provide a final scorecard in a valid JSON object with the following structure:
        1. "overall_summary" (string): A 2-3 sentence paragraph summarizing the candidate's performance.
        2. "strengths" (array of strings): A list of 2-3 key strengths demonstrated by the candidate, specifically related to the job requirements.
        3. "areas_for_improvement" (array of strings): A list of 2-3 constructive areas for improvement.
        4. "final_recommendation" (string): Your final hiring recommendation. Choose one of: "Strongly Recommend", "Recommend", "Consider with Reservations", "Do Not Recommend".
        """
        
        response = model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        scorecard_data = json.loads(cleaned_response_text)
        
        # --- PDF Generation ---
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72)
        
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='TitleStyle', fontName='Helvetica-Bold', fontSize=24, alignment=TA_CENTER, spaceAfter=20))
        styles.add(ParagraphStyle(name='Heading1Style', fontName='Helvetica-Bold', fontSize=16, spaceBefore=12, spaceAfter=6, textColor=navy))
        styles.add(ParagraphStyle(name='Heading2Style', fontName='Helvetica-Bold', fontSize=12, spaceBefore=10, spaceAfter=4))
        styles.add(ParagraphStyle(name='BulletStyle', leftIndent=20, spaceBefore=2))
        styles.add(ParagraphStyle(name='WarningStyle', leftIndent=20, spaceBefore=2, textColor=red))

        story = []
        
        story.append(Paragraph("Candidate Performance Report", styles['TitleStyle']))
        
        story.append(Paragraph("Overall Summary", styles['Heading1Style']))
        story.append(Paragraph(scorecard_data.get('overall_summary', 'Not available.'), styles['Normal']))
        story.append(Spacer(1, 24))

        story.append(Paragraph("Key Strengths", styles['Heading1Style']))
        for strength in scorecard_data.get('strengths', []):
            story.append(Paragraph(f"• {strength}", styles['BulletStyle']))
        story.append(Spacer(1, 24))

        story.append(Paragraph("Areas for Improvement", styles['Heading1Style']))
        for area in scorecard_data.get('areas_for_improvement', []):
            story.append(Paragraph(f"• {area}", styles['BulletStyle']))
        story.append(Spacer(1, 24))

        story.append(Paragraph("Final Recommendation", styles['Heading1Style']))
        story.append(Paragraph(f"<b>{scorecard_data.get('final_recommendation', 'Not available.')}</b>", styles['Normal']))
        story.append(Spacer(1, 24))

        if proctoring_flags:
            story.append(HRFlowable(width="100%", thickness=1, color=black))
            story.append(Spacer(1, 12))
            story.append(Paragraph("Proctoring Flags", styles['Heading1Style']))
            unique_flags = sorted(list(set(proctoring_flags)))
            for flag in unique_flags:
                story.append(Paragraph(f"• {flag}", styles['WarningStyle']))

        doc.build(story)
        buffer.seek(0)

        return Response(buffer, mimetype='application/pdf', headers={
            'Content-Disposition': 'attachment;filename=Candidate_Report.pdf'
        })

    except Exception as e:
        print(f"Error in generate_final_scorecard: {e}")
        return jsonify({'error': f'An unexpected error occurred during final evaluation: {str(e)}'}), 500