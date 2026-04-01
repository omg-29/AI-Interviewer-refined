import os
import uvicorn
import json
import asyncio
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from config import GEMINI_API_KEY
from services.parser import parser
from managers.socket_manager import ConnectionManager

# ---------------- CONFIG ----------------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

app = FastAPI()
manager = ConnectionManager()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions = {}

# ---------------- UTIL ----------------
def safe_json_parse(text):
    try:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
        return json.loads(text)
    except:
        return None

async def ai_call(prompt, retries=2):
    for _ in range(retries):
        try:
            response = model.generate_content(prompt)
            return response.text
        except:
            await asyncio.sleep(1)
    return None

# ---------------- RESUME ANALYSIS ----------------
@app.post("/analyze-resume")
async def analyze_resume(file: UploadFile = File(...)):
    try:
        if not file.filename.endswith(".pdf"):
            return {"error": "Only PDF supported"}

        content = await file.read()
        resume_text = await parser.parse(content)

        if not resume_text or len(resume_text.strip()) < 50:
            return {"error": "Invalid or empty resume"}

        prompt = f"""
You are a STRICT ATS + Interview Analyzer.

Return ONLY JSON:

{{
  "role": "",
  "ats_score": 0,
  "quality_score": 0,
  "is_good": true,
  "interview_topics": [],
  "key_skills": [],
  "weak_areas": [],
  "improvements": []
}}

RULES:
- Be strict (real hiring standards)
- If resume is bad → is_good = false
- No hallucination
- Max 6 interview topics

Resume:
{resume_text[:3500]}
"""

        raw = await ai_call(prompt)
        if not raw:
            return {"error": "AI service unavailable"}

        data = safe_json_parse(raw)
        if not data:
            return {"error": "AI parsing failed"}

        # ---------- FALLBACKS ----------
        role = data.get("role", "Software Engineer")
        if len(role) > 40 or len(role) < 2:
            role = "Software Engineer"

        ats_score = data.get("ats_score", 0)
        quality_score = data.get("quality_score", 0)
        is_good = data.get("is_good", False)

        # ---------- REJECTION ----------
        if ats_score < 60 or quality_score < 50 or not is_good:
            return {
                "rejected": True,
                "message": "Resume not shortlisted",
                "feedback": data
            }

        # ---------- GENERATE JD ----------
        jd_prompt = f"""
Generate a concise job description for a {role}.
Include skills and responsibilities.
"""
        jd = await ai_call(jd_prompt) or ""

        # ---------- SESSION ----------
        if len(sessions) > 100:
            sessions.pop(next(iter(sessions)))

        session_id = f"session_{os.urandom(4).hex()}"
        sessions[session_id] = {
            "resume_text": resume_text,
            "role": role,
            "job_description": jd,
            "ats_data": data,
            "interview_topics": data.get("interview_topics", []),
            "conversation": [],
            "chat": None
        }

        return {
            "session_id": session_id,
            "role": role,
            "ats_score": ats_score,
            "message": "Accepted"
        }

    except Exception as e:
        return {"error": str(e)}

# ---------------- INTERVIEW ----------------
@app.websocket("/ws/interview/{session_id}")
async def interview(websocket: WebSocket, session_id: str):
    await manager.connect(websocket)
    session = sessions.get(session_id)

    if not session:
        await websocket.close()
        return

    # FIXED: session_data → session
    system_prompt = f"""
You are a professional interviewer at a MAANG Company.

Candidate Resume:
{session['resume_text']}

Target Role:
{session['role']}

Job Description:
{session['job_description']}

RULES:
- Start with 1-2 basic questions like "Tell me about Yourself" or "Introduce Yourself."
- Ask ONE question at a time
- Ask follow-ups
- Base questions ONLY on resume + JD
- Adjust difficulty dynamically
- Simplify if struggling
- Go deeper if strong
- Keep answers reasonable length
- Do not change topic and question unless user gives up and politely requests to do so, not just to skip.
- Confirm before ending interview twice, and never continue once terminated by user, even if they say to do so.
"""

    chat = model.start_chat(history=[
        {"role": "user", "parts": [system_prompt]},
        {"role": "model", "parts": ["Ready"]}
    ])

    session["chat"] = chat

    await manager.send_personal_message(
        json.dumps({"type": "text", "content": "Let's start the interview."}),
        websocket
    )

    try:
        while True:
            try:
                data = json.loads(await websocket.receive_text())
            except:
                continue

            if data.get("type") == "transcript":
                user_text = data["content"]

                session["conversation"].append(f"User: {user_text}")
                response = chat.send_message(user_text)
                ai_text = response.text
                session["conversation"].append(f"AI: {ai_text}")

                await manager.send_personal_message(
                    json.dumps({"type": "text", "content": ai_text}),
                    websocket
                )

    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ---------------- FINAL REPORT ----------------
@app.post("/end-interview/{session_id}")
async def end_interview(session_id: str):
    session = sessions.get(session_id)

    if not session:
        return {"error": "Session not found"}

    conversation = "\n".join(session["conversation"])

    # FIXED missing variables
    conversation_text = conversation
    topics_from_resume = ", ".join(session.get("interview_topics", []))
    duration = len(session["conversation"]) // 2
    coding_section = ""

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

    ### Resume ATS Score: {session['ats_data']['ats_score']}
    Resume Quality Score: {session['ats_data']['quality_score']}
    
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

    ### Strengths observed: (Strictly based on the Transcript and overall performance of the candidate)
    
    ### Recommendations for Improvement
    (Base these ONLY on weaknesses actually observed in the interview)
    1. [Specific recommendation based on actual gap shown]
    2. [Specific recommendation based on actual gap shown]
    3.  Resume Improvements:
    {session['ats_data']['improvements']}
    
    ### Topics NOT Covered (From Resume)
    Compare the "Topics from Resume" list above with what was actually discussed.
    List any resume topics that were NOT discussed during the interview as "Not Assessed":
    - [Topic from resume]: Not Assessed
    (This helps identify gaps in the interview coverage - do NOT score these)
    
    Format as clean markdown. Be HONEST and BASE EVERYTHING on the actual transcript above and SCORE according to performance, not to make user Happy even if the interview went bad.
    """

    report = await ai_call(prompt)

    return {
        "report": report,
        "ats_score": session['ats_data']['ats_score']
    }

# ---------------- RUN ----------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
