#!/data/data/com.termux/files/usr/bin/bash
# Build: minify CSS/JS inline and push to Google Apps Script via clasp
set -e
cd "$(dirname "$0")"

python3 -c "
import re

with open('Index.html') as f:
    html = f.read()

# Extract and minify CSS from <style> block
css_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
js_match = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
css = css_match.group(1) if css_match else ''
js = js_match.group(1) if js_match else ''

# Minify CSS
css_min = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
css_min = re.sub(r'\s+', ' ', css_min)
css_min = re.sub(r'\s*([{}:;,])\s*', r'\1', css_min)
css_min = css_min.strip()

# Minify JS (preserve // inside strings)
js_min = re.sub(r'/\*.*?\*/', '', js, flags=re.DOTALL)
js_min = re.sub(r'\n\s*\n', '\n', js_min)
js_min = re.sub(r'^[ \t]+', '', js_min, flags=re.MULTILINE)
js_min = js_min.strip()

# Build with inline CSS and JS
parts = html[:css_match.start()] + '<style>' + css_min + '</style>' + html[css_match.end():js_match.start()] + '<script>' + js_min + '</script>' + html[js_match.end():]
parts = re.sub(r'\n\s*\n', '\n', parts)

with open('Index.html', 'w') as f:
    f.write(parts)

print(f'Built: {len(parts)} bytes')
"

clasp push
clasp deploy --deploymentId AKfycbwiA5CmmxSGT02wvNUjYQXeAeMhh70tUHv0EUV9eHiuJqnX0VuzaHiCjv8Y1nRXdbV7mw --description "build $(date +%Y%m%d-%H%M%S)"
echo "Deployed."
