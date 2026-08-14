import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.environ["Deepseek_api"],
    base_url="https://api.deepseek.com",
)

diff = """
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,5 +1,6 @@
 def login(user):
+    password = "admin123"
     if user.is_active:
         return True
     return False
"""

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    response_format={"type": "json_object"},
    messages=[
        {
            "role": "system",
            "content": ( "You are an experienced code reviewer. "
                "Find real bugs, security issues, missing edge cases, or bad logic in PR diffs. "
                "Return ONLY valid JSON with this shape: "
                '{"issues":[{"file":"path/to/file.py","line":123,"severity":"high|medium|low","comment":"feedback"}]}. '
                'If there are no issues, return {"issues":[]}.'
            ),
        },
        {
            "role": "user",
            "content": f"Review this pull request diff:\n\n{diff}",
        },
    ],
)

print(response.choices[0].message.content)