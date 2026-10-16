"""Download the web fonts render.mjs uses into .fonts/fonts.css (data: URIs, git-ignored).

Rendering then needs no network. All fonts are from Google Fonts under the SIL Open Font License.
    python3 docs/brand/src/fetch_fonts.py
"""
import base64
import pathlib
import re
import subprocess

URL = ("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700"
       "&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600"
       "&family=Heebo:wght@400;500;700&family=Noto+Sans+Math&display=block")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KEEP = ("latin", "hebrew", "math", "symbols", "")   # subsets the images use ("" = font not split)


def get(url: str) -> bytes:
    return subprocess.run(["curl", "-fsSL", "--retry", "5", "-A", UA, url], check=True, capture_output=True).stdout


css = get(URL).decode()
blocks = re.findall(r"(?:/\* ([^*]+) \*/\s*)?(@font-face \{.*?\})", css, re.S)
out = []
for subset, block in blocks:
    if subset.strip() not in KEEP:
        continue
    src = re.search(r"url\((https://[^)]+)\)", block).group(1)
    data = base64.b64encode(get(src)).decode()
    out.append(block.replace(src, f"data:font/woff2;base64,{data}"))
dest = pathlib.Path(__file__).with_name(".fonts")
dest.mkdir(exist_ok=True)
(dest / "fonts.css").write_text("\n".join(out))
print(f"{len(out)} font faces -> {dest / 'fonts.css'}")
