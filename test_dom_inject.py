#!/usr/bin/env python3
"""Test the continuous DOM reading animation.
Opens Chrome and runs the reading highlight for 8 seconds, then stops.
Make sure Chrome has a text-heavy page open (like the UIST site)."""

import subprocess, time

def chrome_js(js: str):
    safe = js.replace('\\', '\\\\').replace('"', '\\"')
    apple = (
        'tell application "Google Chrome"\n'
        '    tell active tab of front window\n'
        f'        execute javascript "{safe}"\n'
        '    end tell\n'
        'end tell'
    )
    subprocess.Popen(["osascript", "-e", apple],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_reading():
    js = (
        "(function(){"
        "if(window._claudeReadTimer)clearInterval(window._claudeReadTimer);"
        "if(window._claudeReadEls)window._claudeReadEls.forEach(function(el){"
        "  el.style.backgroundColor='';el.style.boxShadow='';el.style.borderLeft='';"
        "});"
        ""
        "if(!document.getElementById('_claude_read_css')){"
        "  var s=document.createElement('style');"
        "  s.id='_claude_read_css';"
        "  s.textContent='"
        "    ._claude_reading {"
        "      background: linear-gradient(90deg,"
        "        rgba(255,195,60,0.0) 0%,"
        "        rgba(255,195,60,0.22) 15%,"
        "        rgba(255,195,60,0.28) 50%,"
        "        rgba(255,195,60,0.22) 85%,"
        "        rgba(255,195,60,0.0) 100%) !important;"
        "      box-shadow: inset 0 -2.5px 0 rgba(255,170,30,0.55) !important;"
        "      border-left: 3px solid rgba(255,170,30,0.7) !important;"
        "      transition: background 0.35s ease, box-shadow 0.35s ease,"
        "                  border-left 0.2s ease !important;"
        "    }"
        "    ._claude_read_done {"
        "      background: rgba(255,195,60,0.06) !important;"
        "      box-shadow: none !important;"
        "      border-left: 3px solid rgba(255,170,30,0.15) !important;"
        "      transition: background 0.6s ease, box-shadow 0.4s ease,"
        "                  border-left 0.4s ease !important;"
        "    }"
        "  ';"
        "  document.head.appendChild(s);"
        "}"
        ""
        "var els=document.querySelectorAll("
        "  'p,li,h1,h2,h3,h4,td,th,figcaption,blockquote,dt,dd,pre'"
        ");"
        "var vh=window.innerHeight;"
        "var visible=[];"
        "els.forEach(function(el){"
        "  var r=el.getBoundingClientRect();"
        "  if(r.top>-10&&r.top<vh&&r.height>8&&r.height<500)"
        "    visible.push(el);"
        "});"
        "window._claudeReadEls=visible;"
        "window._claudeReadIdx=0;"
        ""
        "window._claudeReadTimer=setInterval(function(){"
        "  var els=window._claudeReadEls;"
        "  var idx=window._claudeReadIdx;"
        "  if(!els||els.length===0)return;"
        ""
        "  if(idx>0&&idx-1<els.length){"
        "    els[idx-1].classList.remove('_claude_reading');"
        "    els[idx-1].classList.add('_claude_read_done');"
        "  }"
        ""
        "  if(idx<els.length){"
        "    els[idx].classList.remove('_claude_read_done');"
        "    els[idx].classList.add('_claude_reading');"
        "  }"
        ""
        "  window._claudeReadIdx=idx+1;"
        ""
        "  if(idx>=els.length){"
        "    els.forEach(function(e){"
        "      e.classList.remove('_claude_reading','_claude_read_done');"
        "    });"
        "    window._claudeReadIdx=0;"
        "  }"
        "},600);"
        ""
        "return 'reading '+visible.length+' elements';"
        "})()"
    )
    chrome_js(js)

def stop_reading():
    js = (
        "(function(){"
        "if(window._claudeReadTimer){"
        "  clearInterval(window._claudeReadTimer);"
        "  window._claudeReadTimer=null;"
        "}"
        "if(window._claudeReadEls){"
        "  window._claudeReadEls.forEach(function(el){"
        "    el.classList.remove('_claude_reading');"
        "    el.classList.add('_claude_read_done');"
        "  });"
        "  setTimeout(function(){"
        "    if(window._claudeReadEls){"
        "      window._claudeReadEls.forEach(function(el){"
        "        el.classList.remove('_claude_read_done');"
        "      });"
        "    }"
        "  },800);"
        "}"
        "})()"
    )
    chrome_js(js)

if __name__ == "__main__":
    print("Starting reading animation...")
    print("Watch Chrome — text elements should highlight one by one.")
    print()

    start_reading()

    for i in range(8):
        time.sleep(1)
        print(f"  Reading... {i+1}s")

    print()
    print("Stopping reading animation...")
    stop_reading()

    time.sleep(1)
    print("Done. Highlights should have faded.")