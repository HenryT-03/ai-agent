import os
import requests
from dotenv import load_dotenv

load_dotenv()

def get_seo_page_report(url: str):
    # THE URL (From your snippet)
    api_url = "https://website-seo-analyzer.p.rapidapi.com/audit/"

    headers = {
        "x-rapidapi-key": os.getenv('RAPID_API_KEY'),
        "x-rapidapi-host": "website_seo_analyzer.p.rapidapi.com",
        "access_token": os.getenv('SEO_ACCESS_TOKEN'),
        "Content-Type": "application/json"
    }
    
    # THE PARAMETERS (The stuff after the '?' in your snippet)
    params = {"url": url}

    try:
        # We use 'requests' because it handles this dictionary-to-header conversion perfectly
        response = requests.get(api_url, headers=headers, params=params, timeout=160)
        
        if response.status_code == 200:
            return response.json()
        else:
            # Return the raw error so the AI knows it's a permission/config issue
            return f"API Error {response.status_code}: {response.text}"
            
    except Exception as e:
        return f"Connection Failed: {str(e)}"