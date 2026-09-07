#!/usr/bin/env python3
"""
Gemini API 測試與測試腳本
可以使用這個腳本快速驗證你的 Gemini API 密鑰和連線狀態。
"""
from __future__ import annotations

import os
import sys
import httpx

def test_gemini_openai_api(api_key: str, model_name: str = "gemini-1.5-flash") -> None:
    """透過 Google 提供的 OpenAI 相容端點測試 Gemini API"""
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    url = f"{base_url}/v1/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": "請用繁體中文自我介紹，並說明你現在是用哪一個 API"
            }
        ],
        "temperature": 0.2
    }
    
    print(f"正在透過 OpenAI 相容端點呼叫 Gemini...")
    print(f"端點 URL: {url}")
    print(f"使用模型: {model_name}")
    print("-" * 50)
    
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            
        if resp.status_code != 200:
            print(f"❌ 呼叫失敗！狀態碼: {resp.status_code}")
            print(f"錯誤訊息: {resp.text}")
            return
            
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print("✅ 呼叫成功！")
        print("模型回覆內容：")
        print("-" * 50)
        print(content)
        print("-" * 50)
        
    except Exception as e:
        print(f"❌ 發生例外錯誤: {e}")

def main() -> None:
    # 優先從環境變數讀取
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    
    if not api_key:
        print("提示：你尚未設定 GEMINI_API_KEY 環境變數。")
        print("請由控制台輸入你的 Gemini API 密鑰（可至 Google AI Studio 免費申請）：")
        api_key = input("API Key: ").strip()
        
    if not api_key:
        print("❌ 未提供 API Key，無法進行測試。")
        sys.exit(1)
        
    # 可選型號如下：
    # - gemini-1.5-flash (推薦，速度快、免費額度高、支援多模態)
    # - gemini-1.5-pro (理解力更強)
    # - gemini-2.0-flash (最新速度優化版)
    test_gemini_openai_api(api_key, "gemini-1.5-flash")

if __name__ == "__main__":
    main()
