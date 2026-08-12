#!/data/data/com.termux/files/usr/bin/bash
# Build: minify CSS/JS and push to Google Apps Script via clasp
set -e
cd "$(dirname "$0")"

python3 -c "
import re

with open('Index.html') as f:
    html = f.read()

# Extract CSS
css_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
if css_match:
    css = css_match.group(1)
    # Minify CSS
    css_min = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    css_min = re.sub(r'\s+', ' ', css_min)
    css_min = re.sub(r'\s*([{}:;,])\s*', r'\1', css_min)
    css_min = css_min.strip()
    with open('Styles.html', 'w') as f:
        f.write(css_min)

# Extract JS
js_match = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
if js_match:
    js = js_match.group(1)
    # Minify JS (basic)
    js_min = re.sub(r'//[^\n]*\n', '\n', js)
    js_min = re.sub(r'/\*.*?\*/', '', js_min, flags=re.DOTALL)
    js_min = re.sub(r'  +', ' ', js_min)
    js_min = re.sub(r'\n\s*\n', '\n', js_min)
    js_min = js_min.strip()

# Build new Index.html with include and minified JS
new_html = html[:css_match.start()] + '<?!= include(\"Styles\"); ?>' + html[css_match.end():js_match.start()] + '<script>' + js_min + '</script>' + html[js_match.end():]

# Remove blank lines and inter-tag whitespace
new_html = re.sub(r'\n\s*\n', '\n', new_html)
new_html = re.sub(r'>\s+<', '><', new_html)

with open('Index.html', 'w') as f:
    f.write(new_html)

print('Built: Styles.html (' + str(len(css_min)) + 'b), Index.html (' + str(len(new_html)) + 'b)')
"

clasp push
clasp deploy --deploymentId AKfycbwiA5CmmxSGT02wvNUjYQXeAeMhh70tUHv0EUV9eHiuJqnX0VuzaHiCjv8Y1nRXdbV7mw --description "build $(date +%Y%m%d-%H%M%S)"
echo "Deployed."
