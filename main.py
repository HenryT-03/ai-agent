import os
import json
import re
from dotenv import load_dotenv
from google import genai
from actions import get_seo_page_report
from prompts import system_prompt

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
model_id = "gemini-2.5-flash"

available_actions = {
    "get_seo_page_report": get_seo_page_report
}

def extract_json(text):
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            json_str = match.group(0)
            json_str = json_str.replace('“', '"').replace('”', '"')
            return [json.loads(json_str)]
        return None
    except json.JSONDecodeError as e:
        print(f"DEBUG: Failed to decode JSON. Raw text: {text}")
        return None

def run_agent(user_query):
    messages = [{"role": "user", "parts": [{"text": user_query}]}]
    
    turn_count = 0
    max_turns = 5

    while turn_count < max_turns:
        print(f"\n--- Loop Turn {turn_count} ---")
        turn_count += 1

        response = client.models.generate_content(
            model=model_id,
            contents=messages,
            config={'system_instruction': system_prompt}
        )

        response_text = response.text
        print(f"AI: {response_text}")

        if "Answer:" in response_text and "Action:" not in response_text:
            return response_text

        json_actions = extract_json(response_text)
        
        if json_actions:
            action = json_actions[0]
            func_name = action['function_name']
            func_args = action['function_parms']

            if func_name in available_actions:
                print(f"DEBUG: Executing {func_name}({func_args})")
                result = available_actions[func_name](**func_args)
                
                messages.append({"role": "model", "parts": [{"text": response_text}]})
                messages.append({"role": "user", "parts": [{"text": f"Action_Response: {result}"}]})
            else:
                print(f"Error: Unknown function {func_name}")
                break
        else:
            return response_text

def validate_action_api():
    try:
        result = get_seo_page_report("https://google.com")
        #print(f"API Validation Result: {result}")
        return isinstance(result, dict) and len(result) > 0
    except Exception:
        return False
    
if __name__ == "__main__":
    if not validate_action_api():
        print("Action API unavailable. Exiting.")
        print("KEY:", os.getenv("RAPID_API_KEY"))
        exit(1)
    else:
        query = input("Query: ")
        final_answer = run_agent(query)
        print("\n****************\nFINAL RESULT:")
        print(final_answer)