# download_images.py
import os
import pathlib
import pandas as pd
import httpx

API_BASE = "https://uc-prod.ihealth-eng.com/v1/uc/food-log"

# Generate request headers (only x-session-token needs to be correct)
def make_headers(session_token: str):
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://ucfe-dev.ihealth-eng.com",
        "referer": "https://ucfe-dev.ihealth-eng.com/",
        "user-agent": "Mozilla/5.0",
        "x-session-token": session_token,
    }

def guess_ext_from_url(url: str) -> str:
    parsed = pathlib.PurePosixPath(url.split("?")[0])
    ext = parsed.suffix.lower()
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        return ext
    return ".jpg"

def extract_links(payload: dict):
    data = payload.get("data", {})
    images = data.get("images", [])
    links = []
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and "link" in item:
                links.append(item["link"])
            elif isinstance(item, str):
                links.append(item)
    elif isinstance(images, dict) and "link" in images:
        links.append(images["link"])
    elif isinstance(images, str):
        links.append(images)
    return links

def main(csv_path: str, out_dir: str, session_token: str):
    csv_file = pathlib.Path(csv_path)
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file does not exist: {csv_file}")

    df = pd.read_csv(csv_file)
    if "FoodLogId" not in df.columns:
        raise ValueError("CSV must contain FoodLogId column")
    if "ImgName" not in df.columns:
        df["ImgName"] = ""

    out_path = pathlib.Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=15) as client:
        for idx, row in df.iterrows():
            fid = str(row["FoodLogId"]).strip()
            if not fid or fid.lower() == "nan":
                continue

            url = f"{API_BASE}/{fid}"
            try:
                resp = client.get(url, headers=make_headers(session_token))
                resp.raise_for_status()
                payload = resp.json()
                links = extract_links(payload)

                saved_files = []
                for i, link in enumerate(links):
                    img_resp = client.get(link)
                    if img_resp.status_code == 200:
                        ext = guess_ext_from_url(link)
                        fname = f"{fid}_{i}{ext}" if len(links) > 1 else f"{fid}{ext}"
                        fpath = out_path / fname
                        fpath.write_bytes(img_resp.content)
                        saved_files.append(fname)

                df.at[idx, "ImgName"] = ";".join(saved_files)
                print(f"[OK] {fid} -> {saved_files}")
            except Exception as e:
                print(f"[ERROR] {fid}: {e}")

    df.to_csv(csv_file, index=False)
    print(f"Processing complete, results written back to {csv_file}")

if __name__ == "__main__":
    # Use environment variable SESSION_TOKEN, or you can directly hardcode the string
    # token = os.environ.get("SESSION_TOKEN", "").strip()

    # Can get it from https://portal.ihealthunifiedcare.com/care-portal/home -> inspect -> application -> session storage -> https://portal.ihealth-eng.com -> token
    # token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzZXNzaW9uSWQiOiJlZmRlMDcwYzk1MGM5Y2VjY2Q0MDk0YjhkNDMxNjhhYzdhMWIzNGYxZjUwYWU0MGVkOTY3Y2FiYjRlM2JjZmNhIiwidXNlclR5cGUiOiJFTVBMT1lFRSIsImV4cCI6MTc1OTYwODUyNCwidXNlcklkIjoiNjhlMDEwYmRkYTlmYmE2NjU1OWY2NDVjIiwiaWF0IjoxNzU5NTIyMTI0LCJlbWFpbCI6ImNoZW5neWFvLnNoZW5AaWhlYWx0aGxhYnMuY29tIn0.clD0fH8tdpDcNevcjBU_CYPYh3zEr0rXzD0c8Lut39E"
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzZXNzaW9uSWQiOiIxYjMzMDVhMWNkYzMwN2Q3MjlmYzA1NjA5MDE3YWY1NDU1ZjIyMjZmMDEwMGI1NzQ2OTgyOWNkOTUwOTIwYmJiIiwidXNlclR5cGUiOiJFTVBMT1lFRSIsImV4cCI6MTc2MTMzOTY3NCwidXNlcklkIjoiNjhlMDEwYmRkYTlmYmE2NjU1OWY2NDVjIiwiaWF0IjoxNzYxMjUzMjc0LCJlbWFpbCI6ImNoZW5neWFvLnNoZW5AaWhlYWx0aGxhYnMuY29tIn0.A8Lx0HQWio6JTf9V8GzJgrfyD2kOFVvb7biLb2XDknE"
    if not token:
        raise RuntimeError("Please set SESSION_TOKEN environment variable first, or modify the code to directly enter the token")

    # Modify to your CSV path
    # csv_path = "foodlog_ai_analysis_img_name.csv"
    csv_path = "foodlog_ai_analysis_v2.csv"
    out_dir = "./images"

    main(csv_path, out_dir, token)
