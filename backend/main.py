import os
import uvicorn
import json
import asyncio
import time
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict

# ----------- QWEN (OpenAI-compatible) -----------
from openai import OpenAI
client = OpenAI(api_key=os.getenv("QWEN_API_KEY"), base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL_NAME = "qwen-plus"

# ----------- APP SETUP -----------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions = {}
last_call_time = defaultdict(float)
request_queues = {}

# ----------- UTIL -----------
def safe_json_parse(text):
    try:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
        return json.loads(text)
    except:
        return None

# ----------- RATE LIMIT -----------
async def rate_limited(session_id, delay=2):
    now = time.time()
    if now - last_call_time[session_id] < delay:
        return False
    last_call_time[session_id] = now
    return True

# ----------- RETRY LOGIC -----------
async def ai_call(messages, retries=5):
    delay = 1
    for _ in range(retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages
            )
            return response.choices[0].message.content
        except Exception as e:
            if "429" in str(e):
                await asyncio.sleep(delay)
                delay *= 2
            else:
                await asyncio.sleep(1)
    return None

# ----------- QUEUE PROCESSOR -----------
async def process_queue(session_id, websocket):
    queue = request_queues[session_id]

    while True:
        user_text = await queue.get()

        if not await rate_limited(session_id):
            queue.task_done()
            continue

        session = sessions[session_id]
        messages = session["messages"]

        messages.append({"role": "user", "content": user_text})

        response = await ai_call(messages)
        if not response:
            queue.task_done()
            continue

        messages.append({"role": "assistant", "content": response})
        session["conversation"].append(f"User: {user_text}")
        session["conversation"].append(f"AI: {response}")

        await websocket.send_text(json.dumps({
            "type": "text",
            "content": response
        }))

        queue.task_done()

# ----------- RESUME ANALYSIS (RESTORED ATS LOGIC) -----------
@app.post("/analyze-resume")
async def analyze_resume(file: UploadFile = File(...)):
    if not file.filename.endswith(".pdf"):
        return {"error": "Only PDF supported"}

    content = await file.read()
    resume_text = await parser.parse(content)
    resume_text = resume_text[:4000]

    if len(resume_text.strip()) < 50:
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
{resume_text}
"""

    raw = await ai_call([
        {"role": "system", "content": "You are an ATS analyzer."},
        {"role": "user", "content": prompt}
    ])

    if not raw:
        return {"error": "AI unavailable"}

    data = safe_json_parse(raw)
    if not data:
        return {"error": "Parsing failed", "raw": raw}

    role = data.get("role", "Software Engineer")
    ats_score = data.get("ats_score", 0)
    quality_score = data.get("quality_score", 0)
    is_good = data.get("is_good", False)

    if ats_score < 60 or quality_score < 50 or not is_good:
        return {
            "rejected": True,
            "message": "Resume not shortlisted",
            "feedback": data
        }

    jd_prompt = f"Generate a concise job description for a {role}."

    jd = await ai_call([
        {"role": "system", "content": "You generate job descriptions."},
        {"role": "user", "content": jd_prompt}
    ]) or ""

    session_id = f"session_{os.urandom(4).hex()}"

    sessions[session_id] = {
        "messages": [
            {
                "role": "system",
                "content": f"""
You are a professional interviewer at a MAANG Company.

Candidate Resume:
{resume_text}

Target Role:
{role}

Job Description:
{jd}

RULES:
- Ask ONE question at a time
- Ask follow-ups
- Base questions ONLY on resume + JD
- Adjust difficulty dynamically
- Keep answers concise
"""
            }
        ],
        "conversation": [],
        "ats_data": data,
        "role": role,
        "job_description": jd,
        "interview_topics": data.get("interview_topics", [])
    }

    return {
        "session_id": session_id,
        "role": role,
        "ats_score": ats_score,
        "message": "Accepted"
    }

# ----------- WEBSOCKET -----------
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    if session_id not in sessions:
        await websocket.close()
        return

    request_queues[session_id] = asyncio.Queue()
    asyncio.create_task(process_queue(session_id, websocket))

    await websocket.send_text(json.dumps({
        "type": "text",
        "content": "Let's start the interview."
    }))

    try:
        while True:
            data = json.loads(await websocket.receive_text())

            if data.get("type") == "transcript":
                text = data.get("content", "").strip()

                # -------- DEBOUNCE --------
                if len(text) < 15:
                    continue

                await request_queues[session_id].put(text)

    except WebSocketDisconnect:
        pass

# ----------- FINAL REPORT (ULTRA DETAILED RESTORED) -----------
@app.post("/end/{session_id}")
async def end_interview(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}

    conversation = "
".join(session["conversation"])
    topics_from_resume = ", ".join(session.get("interview_topics", []))
    duration = len(session["conversation"]) // 2

    prompt = f"""
The interview is complete. Generate a Report Card based STRICTLY on the actual conversation below.
    
    ============ ACTUAL INTERVIEW TRANSCRIPT ============
    {conversation}
    =====================================================
    Topics from Resume: {topics_from_resume}
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
    
    ### Communication Quality
    - Clarity: [1-10]/10
    - Depth of Answers: [1-10]/10
    - Technical Vocabulary: [1-10]/10
    
    ### Key Quotes from Interview
    Include 2-3 notable quotes (good or bad) from the candidate's actual responses.

    ### Strengths observed
    (Strictly based on the Transcript and overall performance)
    
    ### Recommendations for Improvement
    1. [Specific recommendation based on actual gap shown]
    2. [Specific recommendation based on actual gap shown]
    3. Resume Improvements:
    {session['ats_data']['improvements']}
    
    ### Topics NOT Covered (From Resume)
    - [Topic from resume]: Not Assessed
    
    Format as clean markdown. Be HONEST and BASE EVERYTHING on the actual transcript above.
    """

    report = await ai_call([
        {"role": "system", "content": "You are an expert interviewer generating strict evaluation reports."},
        {"role": "user", "content": prompt}
    ])

    return {
        "report": report,
        "ats_score": session['ats_data']['ats_score']
    }

# ----------- RUN -----------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
       "report": report,
        "ats_score": session['ats_data']['ats_score']
    }

# ---------------- RUN ----------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
