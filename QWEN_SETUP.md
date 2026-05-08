# Qwen Local Model Branch

This branch uses **Qwen2** local model inference via Ollama instead of the Gemini API.

## Setup Instructions

### 1. Install Ollama

Download and install Ollama from https://ollama.ai

### 2. Pull the Qwen Model

```bash
ollama pull qwen2
# or for larger model:
ollama pull qwen2:13b
```

### 3. Start Ollama Server

```bash
ollama serve
```

This will start the Ollama API server on `http://localhost:11434` by default.

### 4. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables (Optional)

Update `.env` file with:

```env
OLLAMA_BASE_URL=http://localhost:11434/v1
MODEL_ID=qwen2
```

If not specified, the code will use these defaults:
- `OLLAMA_BASE_URL`: `http://localhost:11434/v1`
- `MODEL_ID`: `qwen2`

## Key Changes from Main Branch

### Dependencies
- ❌ Removed: `google-genai` (Gemini API)
- ✅ Added: `openai` (for OpenAI-compatible API via Ollama)

### Model Initialization
Old (main branch):
```python
from google import genai
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL_ID = "gemini-2.5-flash-lite"
```

New (qwen branch):
```python
from openai import OpenAI
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL)
MODEL_ID = os.getenv("MODEL_ID", "qwen2")
```

### API Call
Old (Gemini API):
```python
client.models.generate_content(
    model=MODEL_ID,
    contents=messages,
    config={"system_instruction": system_instruction},
)
```

New (OpenAI-compatible via Ollama):
```python
client.chat.completions.create(
    model=MODEL_ID,
    messages=openai_messages,
    temperature=0.7,
    max_tokens=4096,
)
```

## Performance Notes

- **Local inference** is slower than cloud APIs but has no latency from API calls
- **Model size** matters: `qwen2:7b` is smaller/faster, `qwen2:13b` is more capable
- **Hardware** requirements: 8-16GB VRAM recommended
- No rate limiting concerns with local models

## Troubleshooting

### Connection Refused Error
Make sure Ollama is running:
```bash
ollama serve
```

### Model Not Found Error
Pull the model:
```bash
ollama pull qwen2
```

### Out of Memory Error
Use a smaller model:
```bash
ollama pull qwen2:7b
# Update MODEL_ID in .env to qwen2:7b
```

## Running the Agent

```bash
python main.py "Your query here"
```

Or interactively:
```bash
python main.py
```
