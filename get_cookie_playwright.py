"""
get_cookie_playwright.py

Opens a browser via Playwright, waits for Roblox login, extracts the .ROBLOSECURITY
cookie, and saves it to cookies.txt on the Desktop (and copies it to clipboard if available).

Requirements:
    pip install playwright pyperclip
    python -m playwright install chromium

Usage:
    python get_cookie_playwright.py
"""

import os
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("Error: Playwright is not installed or not found.")
    print("Install it with: pip install playwright")
    print("Then: python -m playwright install chromium")
    sys.exit(1)

try:
    import pyperclip
    HAVE_PYPERCLIP = True
except Exception:
    HAVE_PYPERCLIP = False


def desktop_cookies_path(filename: str = "cookies.txt") -> str:
    """Return the path to the file on the user's Desktop (cross-platform)."""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    if not os.path.isdir(desktop):
        desktop = home
    return os.path.join(desktop, filename)


def write_cookie_to_desktop(cookie_value: str, filename: str = "cookies.txt") -> str:
    path = desktop_cookies_path(filename)
    mode = "a" if os.path.exists(path) else "w"
    try:
        with open(path, mode, encoding="utf-8") as f:
            f.write(cookie_value.strip() + "\n")
    except Exception as e:
        raise RuntimeError(f"Failed to write file {path}: {e}")
    return path


def find_roblosecurity_from_cookies(cookies: list) -> str | None:
    for c in cookies:
        if c.get("name") == ".ROBLOSECURITY":
            return c.get("value")
    return None


def main():
    print("=== Roblox .ROBLOSECURITY helper (Playwright) ===")
    print("A browser window will open. Log in to your Roblox account there.")
    print("Once logged in, return here and press Enter to extract the cookie.")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto("https://www.roblox.com/login")
        except Exception as e:
            print("Failed to open https://www.roblox.com/:", e)
            browser.close()
            sys.exit(1)

        input("After logging in, press Enter in this console...")
        time.sleep(1.0)

        try:
            cookies = context.cookies()
        except Exception as e:
            print("Error while getting cookies from the browser context:", e)
            browser.close()
            sys.exit(1)

        roblosec = find_roblosecurity_from_cookies(cookies)
        browser.close()

    if not roblosec:
        print("❌ .ROBLOSECURITY not found.")
        print("Possible reasons:")
        print("- You didn't log in during the opened browser session.")
        print("- Roblox uses a different cookie scope (rare).")
        print("- The page blocked cookie access.")
        sys.exit(1)

    try:
        out_path = write_cookie_to_desktop(roblosec, filename="cookies.txt")
    except Exception as e:
        print("Error writing cookie to file:", e)
        sys.exit(1)

    if HAVE_PYPERCLIP:
        try:
            pyperclip.copy(roblosec)
            clipboard_msg = " (copied to clipboard)"
        except Exception:
            clipboard_msg = " (failed to copy to clipboard)"
    else:
        clipboard_msg = " (pyperclip not installed — clipboard unavailable)"

    print(f"✅ .ROBLOSECURITY found and saved to: {out_path}")
    print(
        "Cookie (first 6 characters):",
        roblosec[:6] + "..." if len(roblosec) > 6 else roblosec,
        clipboard_msg,
    )
    print()
    print(
        "You can rerun this script to add another cookie — it appends new entries to the same file."
    )


if __name__ == "__main__":
    main()
