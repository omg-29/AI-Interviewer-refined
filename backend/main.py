import os
import uvicorn
import json
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from config import GEMINI_API_KEY
from services.parser import parser
from services.tts import tts_service
from managers.socket_manager import ConnectionManager

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)
# Using gemma-3-1b-it - works without quota issues
model = genai.GenerativeModel('gemma-3-1b-it')

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()

# Store session data in memory for MVP (use Redis/Supabase in prod)
# sessionId -> { resume_text: str, chat_session: ChatSession }
sessions = {}

@app.post("/analyze-resume")
async def analyze_resume(file: UploadFile = File(...)):
    try:
        if not file.filename.endswith('.pdf'):
            return {"error": "Only PDF files are supported"}
        
        content = await file.read()
        resume_text = await parser.parse(content)

        if not resume_text or len(resume_text.strip()) < 50:
            return {"error": "Resume text extraction failed or too short"}

        # -------------------------------
        # STEP 1: Validate Resume
        # -------------------------------
        validation_prompt = f"""
        Is this a resume? Answer ONLY: RESUME or NOT_RESUME

        {resume_text[:1500]}
        """
        validation = model.generate_content(validation_prompt).text.strip().upper()

        if "RESUME" not in validation:
            return {"error": "Invalid resume uploaded"}

        # -------------------------------
        # STEP 2: Extract Role (SAFE)
        # -------------------------------
        role_prompt = f"""
        Extract the most suitable job role from this resume.

        Rules:
        - Only output role name
        - Max 5 words
        - No explanation

        Resume:
        {resume_text[:2000]}
        """

        role = model.generate_content(role_prompt).text.strip()

        if len(role) > 40 or len(role) < 2:
            role = "Software Engineer"   # fallback

        # -------------------------------
        # STEP 3: Generate Job Description
        # -------------------------------
        jd_prompt = f"""
        Create a realistic job description for a {role}.

        Include:
        - Responsibilities
        - Required skills
        - Experience level

        Keep it generic but industry-relevant.
        """

        jd = model.generate_content(jd_prompt).text.strip()

        # -------------------------------
        # STEP 4: ATS EVALUATION (CRITICAL)
        # -------------------------------
        ats_prompt = f"""
        You are an ATS system.

        Evaluate this resume strictly.

        Return ONLY JSON:

        {{
          "ats_score": number,
          "quality_score": number,
          "is_good": true/false,
          "issues": [],
          "missing_sections": [],
          "improvements": []
        }}

        Resume:
        {resume_text}

        Job Description:
        {jd}
        """

        ats_raw = model.generate_content(ats_prompt).text

        try:
            ats_data = json.loads(ats_raw)
        except:
            return {"error": "ATS parsing failed"}

        # -------------------------------
        # STEP 5: REJECTION FILTER
        # -------------------------------
        if (
            ats_data.get("ats_score", 0) < 60 or
            ats_data.get("quality_score", 0) < 50 or
            not ats_data.get("is_good", False)
        ):
            return {
                "rejected": True,
                "message": "Resume not suitable for interview",
                "ats_feedback": ats_data
            }

        # -------------------------------
        # STEP 6: ANALYSIS FOR INTERVIEW
        # -------------------------------
        analysis_prompt = f"""
        Analyze resume for interview preparation.

        Return ONLY JSON:

        {{
          "key_skills": [],
          "experience_level": "",
          "interview_topics": [],
          "strengths": [],
          "weak_areas": []
        }}

        Resume:
        {resume_text}

        Job Description:
        {jd}
        """

        analysis_raw = model.generate_content(analysis_prompt).text

        try:
            analysis = json.loads(analysis_raw)
        except:
            analysis = {}

        interview_topics = analysis.get("interview_topics", [])

        # Filter topics
        interview_topics = [
            t for t in interview_topics if isinstance(t, dict)
        ][:6]

        # -------------------------------
        # SESSION
        # -------------------------------
        session_id = f"session_{os.urandom(4).hex()}"

        sessions[session_id] = {
            "resume_text": resume_text,
            "job_description": jd,
            "role": role,
            "ats_data": ats_data,
            "analysis": analysis,
            "interview_topics": interview_topics,
            "conversation": [],
            "chat": None
        }

        return {
            "session_id": session_id,
            "role": role,
            "ats_score": ats_data["ats_score"],
            "message": "Resume accepted"
        }

    except Exception as e:
        return {"error": str(e)}
        
@app.websocket("/ws/interview/{session_id}")
async def interview_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket)
    
    session_data = sessions.get(session_id)
    if not session_data:
        await websocket.close(code=4004, reason="Session not found")
        return

    # Get interview settings from query params
    query_params = dict(websocket.query_params)
    persona = query_params.get("persona", "balanced")
    interview_type = query_params.get("type", "mixed")
    difficulty = query_params.get("difficulty", "mid")
    duration = int(query_params.get("duration", "15"))  # Duration in minutes
    
    # Store duration in session for report generation
    session_data["duration"] = duration
    session_data["code_submissions"] = []  # Track code submissions
    
    # Get interview topics and calculate time per topic
    interview_topics = session_data.get("interview_topics", [])
    num_topics = len(interview_topics) if interview_topics else 5
    minutes_per_topic = max(2, duration // num_topics)  # At least 2 minutes per topic
    
    # Persona-specific behaviors
    persona_traits = {
        "friendly": {
            "name": "Shreya",
            "style": "warm, supportive, and encouraging. Give positive feedback frequently. Help candidates when they struggle.",
            "greeting": "Hi there! I'm Shreya. Thanks so much for joining me today! I've had a chance to look at your resume - really impressive stuff! How are you feeling today?"
        },
        "balanced": {
            "name": "Shreya", 
            "style": "professional, fair, and constructive. Give balanced feedback. Ask follow-up questions to probe deeper.",
            "greeting": "Hello! I'm Shreya. Thanks for joining me today. I've reviewed your resume, and it looks good. How are you doing today?"
        },
        "strict": {
            "name": "Shreya",
            "style": "rigorous, challenging, and demanding. Push candidates to think harder. Ask tough follow-up questions. Don't accept vague answers.",
            "greeting": "Good day. I'm Shreya, and I'll be conducting your technical interview. I've reviewed your resume. Let's get started - we have limited time."
        }
    }
    
    traits = persona_traits.get(persona, persona_traits["balanced"])
    
    # Coding question instructions for longer interviews (>=10 minutes)
    coding_instructions = ""
    if duration >= 10 and interview_type in ["technical", "mixed"]:
        coding_instructions = f"""
    
    CODING QUESTION REQUIREMENT:
    Since this is a {duration}-minute interview, you MUST ask at least ONE coding problem.
    - Ask a coding question appropriate for {difficulty} level (e.g., array manipulation, string processing, algorithm design)
    - Clearly state the problem, input format, and expected output
    - Tell the candidate to use the code editor on the right side of the screen to write their solution
    - Tell them to click 'Submit Code' when they are done
    - After they submit, you will receive their code and should provide feedback on it
    - Evaluate: correctness, code quality, efficiency, and edge case handling
    """
    
    # Build topics coverage instruction
    topics_instruction = ""
    if interview_topics:
        topics_list = "\n".join([f"    - [{t.get('priority', 'medium').upper()}] {t.get('topic', 'Unknown')} ({t.get('category', 'general')})" for t in interview_topics])
        topics_instruction = f"""
    
    TOPICS TO COVER (from candidate's resume):
    You have {duration} minutes total. Aim to spend ~{minutes_per_topic} minutes per topic.
{topics_list}
    
    TOPIC COVERAGE STRATEGY:
    1. Start with HIGH priority topics first
    2. Naturally transition between topics - don't abruptly switch
    3. Ask 1-2 questions per topic before moving on
    4. If the candidate demonstrates strong knowledge, briefly acknowledge and move to next topic
    5. If they struggle, probe a bit deeper but don't get stuck - move on after 2-3 attempts
    6. Ensure you cover at least the HIGH and MEDIUM priority topics
    7. Keep track mentally of which topics you've covered
    8. Near the end of the interview, if you haven't covered important topics, ask about them directly
    """
    
    # Initialize Chat Session with Persona
    system_prompt = f"""
You are a professional interviewer at a MAANG Company.

Candidate Resume:
{session_data['resume_text']}

Target Role:
{session_data['role']}

Job Description:
{session_data['job_description']}

RULES:
- Start the main interview after asking 1-2 basic questions like "Tell me about yourself?" or "Introduce Yourself." to make a smooth start.
- Ask ONE question at a time.
- Ask 1-2 follow ups based on the users response to validate the grasp on the technical topic.
- Base questions ONLY on resume + JD
- Adjust difficulty dynamically
- If candidate struggles → simplify
- If strong → go deeper
- Keep answers of reasonable length.
- Never change the question or topic if user tells to do so normally, until and unless the user says that they cannot answer the question or does not know about the topic.
- If the user asks to end the interview, first ask for confirmation, if they agrees then tell them to click the end interview button and do not start the interview again even if they say to do so. Because once the interview is over, it means it's over.

"""
    
    chat = model.start_chat(history=[
        {"role": "user", "parts": [system_prompt]},
        {"role": "model", "parts": [f"Understood. I am {traits['name']}. I am ready to interview the candidate."]}
    ])
    session_data["chat"] = chat

    # Initial greeting from AI
    greeting = traits["greeting"]
    # Send text
    await manager.send_personal_message(json.dumps({"type": "text", "content": greeting}), websocket)
    # Send audio
    audio_bytes = await tts_service.generate_audio(greeting)
    await websocket.send_bytes(audio_bytes) 

    try:
        while True:
            data = await websocket.receive_text()
            # client sends JSON: { "type": "transcript", "content": "..." }
            message_data = json.loads(data)
            
            # Handle ping messages to keep connection alive
            if message_data.get("type") == "ping":
                await manager.send_personal_message(json.dumps({"type": "pong"}), websocket)
                continue
            
            # Handle code submissions
            if message_data.get("type") == "code_submission":
                code = message_data.get("code", "")
                language = message_data.get("language", "unknown")
                
                # Store the code submission
                session_data["code_submissions"].append({
                    "code": code,
                    "language": language
                })
                
                # Send code to AI for evaluation
                code_prompt = f"""The candidate has submitted their code solution:

Language: {language}
```{language}
{code}
```

Please review this code and provide brief feedback on:
1. Does it look correct for the problem asked?
2. Code quality and readability
3. Any suggestions for improvement

Keep your response concise (2-4 sentences)."""
                
                # Store in conversation
                if "conversation" not in session_data:
                    session_data["conversation"] = []
                session_data["conversation"].append(f"User submitted code ({language}):\n{code}")
                
                # Get AI response
                response = chat.send_message(code_prompt)
                ai_text = response.text
                
                # Store AI response
                session_data["conversation"].append(f"AI: {ai_text}")
                
                # Send Text
                await manager.send_personal_message(json.dumps({"type": "text", "content": ai_text}), websocket)
                
                # Generate and Send Audio
                audio_data = await tts_service.generate_audio(ai_text)
                await websocket.send_bytes(audio_data)
                continue
            
            if message_data.get("type") == "transcript":
                user_text = message_data["content"]
                
                # Store conversation in session for report generation
                if "conversation" not in session_data:
                    session_data["conversation"] = []
                session_data["conversation"].append(f"User: {user_text}")
                
                # Get AI response
                response = chat.send_message(user_text)
                ai_text = response.text
                
                # Store AI response
                session_data["conversation"].append(f"AI: {ai_text}")
                
                # Send Text
                await manager.send_personal_message(json.dumps({"type": "text", "content": ai_text}), websocket)
                
                # Generate and Send Audio
                audio_data = await tts_service.generate_audio(ai_text)
                await websocket.send_bytes(audio_data)

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print(f"Client #{session_id} left")
    except Exception as e:
        print(f"Error: {e}")
        manager.disconnect(websocket)

@app.post("/end-interview/{session_id}")
async def end_interview(session_id: str):
    session_data = sessions.get(session_id)
    if not session_data or not session_data.get("chat"):
        return {"error": "Session not found or chat not initialized"}
    
    chat = session_data["chat"]
    skill_gaps = session_data.get("skill_gaps", "No skill gap data available")
    code_submissions = session_data.get("code_submissions", [])
    
    # Get the actual conversation that happened
    conversation_history = session_data.get("conversation", [])
    conversation_text = "\n".join(conversation_history) if conversation_history else "No conversation recorded"
    
    # Get interview duration and topics covered
    duration = session_data.get("duration", 15)
    interview_topics = session_data.get("interview_topics", [])
    
    # Build coding assessment section if code was submitted
    coding_section = ""
    if code_submissions:
        coding_section = """
    ### Coding Assessment
    The candidate submitted code during the interview. Evaluate ONLY the submitted code:
    - Code Correctness: [1-10]/10 (Does the code solve the problem asked?)
    - Code Quality: [1-10]/10 (Readability, naming, structure)
    - Efficiency: [1-10]/10 (Time/space complexity considerations)
    - Edge Cases: [1-10]/10 (Did they handle edge cases?)
    - Overall Coding Score: [1-10]/10
    (Provide specific feedback on their actual submitted code)
    """
        # Append code submissions to prompt for context
        for i, submission in enumerate(code_submissions, 1):
            coding_section += f"\n    Code Submission {i} ({submission['language']}):\n    ```{submission['language']}\n    {submission['code']}\n    ```\n"
    
    # Build topics list from resume for comparison
    topics_from_resume = ""
    if interview_topics:
        topics_list = "\n".join([f"    - {t.get('topic', 'Unknown')} ({t.get('priority', 'medium')} priority)" for t in interview_topics])
        topics_from_resume = f"""
    ============ TOPICS FROM RESUME (Expected to be covered) ============
{topics_list}
    ======================================================================
    """
    
    # Generate Report using Gemini with explicit conversation context
    prompt = f"""
    The interview is complete. Generate a Report Card based STRICTLY on the actual conversation below.
    
    ============ ACTUAL INTERVIEW TRANSCRIPT ============
    {conversation_text}
    =====================================================
    {topics_from_resume}
    Interview Duration: {duration} minutes
    
    ⚠️ CRITICAL INSTRUCTIONS - READ CAREFULLY:
    1. ONLY evaluate topics and skills that were ACTUALLY DISCUSSED in the transcript above
    2. DO NOT mention or score ANY topic that was not explicitly covered in the conversation
    3. DO NOT assume knowledge or skills - only assess what the candidate actually demonstrated
    4. If a topic wasn't discussed, DO NOT include it in strengths, weaknesses, or recommendations
    5. Quote specific responses from the transcript to justify your scores
    6. If the interview was short or limited in scope, reflect that honestly in your assessment
    7. Compare the "Topics from Resume" list with the actual transcript to identify what was NOT covered
    
    SCORING GUIDELINES (be honest and specific):
    - 1-3: Poor (wrong answers, fundamental misunderstandings, couldn't answer)
    - 4-5: Below Average (partial knowledge, struggled to explain, major gaps)
    - 6-7: Average (correct but basic answers, could go deeper)
    - 8-9: Good (strong understanding, clear explanations, good examples)
    - 10: Excellent (exceptional depth, went above and beyond)
    
    ## INTERVIEW REPORT CARD
    
    ### Overall Score: [X.X]/10
    (Justify based on SPECIFIC responses from the transcript)
    
    ### Topics Actually Covered
    List ONLY the topics that were discussed with brief assessment:
    - [Topic 1]: [Brief assessment based on actual response]
    - [Topic 2]: [Brief assessment based on actual response]
    (Only include topics from the actual conversation)
    
    ### Technical Assessment
    - Demonstrated Proficiency: (Junior/Mid/Senior - based ONLY on actual answers)
    - Strengths Shown: (2-3 specific examples with quotes from transcript)
    - Weaknesses Identified: (2-3 specific gaps shown in actual responses)
    {coding_section}
    
    ### Communication Quality
    - Clarity: [1-10]/10 (How clearly did they explain in their actual responses?)
    - Depth of Answers: [1-10]/10 (Did they provide detailed or superficial answers?)
    - Technical Vocabulary: [1-10]/10 (Did they use correct terminology?)
    
    ### Key Quotes from Interview
    Include 2-3 notable quotes (good or bad) from the candidate's actual responses.
    
    ### Recommendations for Improvement
    (Base these ONLY on weaknesses actually observed in the interview)
    1. [Specific recommendation based on actual gap shown]
    2. [Specific recommendation based on actual gap shown]
    
    ### Topics NOT Covered (From Resume)
    Compare the "Topics from Resume" list above with what was actually discussed.
    List any resume topics that were NOT discussed during the interview as "Not Assessed":
    - [Topic from resume]: Not Assessed
    (This helps identify gaps in the interview coverage - do NOT score these)
    
    Format as clean markdown. Be HONEST and BASE EVERYTHING on the actual transcript above.
    """
    
    response = chat.send_message(prompt)
    report = response.text
    
    return {"report": report, "skill_gaps": skill_gaps}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
