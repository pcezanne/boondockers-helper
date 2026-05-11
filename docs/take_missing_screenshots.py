#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
take_missing_screenshots.py — Capture the two remaining dashboard screenshots:
  - charging table with Type dropdown open
  - SOC chart with hover tooltip over a noted session

Usage:
    python3 docs/take_missing_screenshots.py
"""

import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

IMAGES_DIR = Path(__file__).parent / 'images'
PORT = 8050
URL = f'http://localhost:{PORT}'


def wait_for_server(page, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            page.goto(URL, timeout=3000, wait_until='domcontentloaded')
            return True
        except Exception:
            time.sleep(0.5)
    return False


def take_missing():
    IMAGES_DIR.mkdir(exist_ok=True)

    server = subprocess.Popen(
        [sys.executable, '-m', 'victron.app', '--no-open'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print('Starting dashboard server...')
    time.sleep(4)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1400, 'height': 2400})

            if not wait_for_server(page):
                print('ERROR: Server did not start')
                return

            page.wait_for_selector('#main-chart', timeout=30000)
            page.wait_for_selector('#charging-table', timeout=30000)
            time.sleep(3)

            # ----------------------------------------------------------------
            # 1. Charging table with Type dropdown open
            #    Dash dcc.Dropdown renders with class "Select" (react-select)
            # ----------------------------------------------------------------
            charging_heading = page.locator('h2', has_text='Charging Sessions')
            charging_heading.scroll_into_view_if_needed()
            time.sleep(0.5)

            # Find dropdown — Dash uses .Select .Select-control or .dash-dropdown
            # Try clicking the first dropdown placeholder/value container
            dropdown = page.locator('#charging-table .dash-dropdown-trigger').first
            if dropdown.count():
                dropdown.click()
                time.sleep(0.8)
                charging_table = page.locator('#charging-table table')
                if charging_table.count():
                    charging_table.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_charging_dropdown_open.png')
                    )
                    print('  dashboard_charging_dropdown_open.png')
                page.keyboard.press('Escape')
                time.sleep(0.3)
            else:
                # Fallback: screenshot the charging table without dropdown open
                # and log what selectors exist
                all_classes = page.evaluate("""() => {
                    const els = document.querySelectorAll('#charging-table *');
                    const classes = new Set();
                    els.forEach(el => el.classList.forEach(c => classes.add(c)));
                    return [...classes].filter(c => c.toLowerCase().includes('select') ||
                                                    c.toLowerCase().includes('drop'));
                }""")
                print(f'  Dropdown-related classes found: {all_classes}')
                # Try clicking by the dropdown input
                inp = page.locator('#charging-table input[type="text"]').first
                if not inp.count():
                    inp = page.locator('#charging-table input').first
                if inp.count():
                    inp.click()
                    time.sleep(0.8)
                    charging_table = page.locator('#charging-table table')
                    if charging_table.count():
                        charging_table.screenshot(
                            path=str(IMAGES_DIR / 'dashboard_charging_dropdown_open.png')
                        )
                        print('  dashboard_charging_dropdown_open.png (via input click)')
                    page.keyboard.press('Escape')

            # ----------------------------------------------------------------
            # 2. SOC chart hover tooltip over a noted session
            #    Scroll to top, then try multiple x positions to find a note hover
            # ----------------------------------------------------------------
            page.evaluate('window.scrollTo(0, 0)')
            time.sleep(0.5)

            chart = page.locator('#main-chart').first
            box = chart.bounding_box()
            if box:
                # Ask Plotly for the page-absolute pixel coordinates of note scatter points
                note_pixels = page.evaluate("""() => {
                    const gd = document.getElementById('main-chart')
                              ?.querySelector('.js-plotly-plot');
                    if (!gd || !gd._fullData) return [];
                    const rect = gd.getBoundingClientRect();
                    const pts = [];
                    for (const trace of gd._fullData) {
                        if (trace.hovertemplate && trace.hovertemplate.includes('<br>') &&
                            trace.marker && trace.marker.size === 16) {
                            // This is a note trace — get pixel coords of first point
                            const xa = gd._fullLayout[trace.xaxis] || gd._fullLayout.xaxis;
                            const ya = gd._fullLayout[trace.yaxis] || gd._fullLayout.yaxis;
                            if (xa && ya && trace.x && trace.x.length) {
                                const x = xa.l2p(xa.d2l(trace.x[0])) + xa._offset + rect.left;
                                const y = ya.l2p(trace.y[0]) + ya._offset + rect.top;
                                pts.push({x, y, text: trace.hovertemplate.slice(0, 60)});
                            }
                        }
                    }
                    return pts;
                }""")
                print(f'  Note scatter pixels: {note_pixels}')

                if note_pixels:
                    # note_pixels are page-absolute client coordinates
                    pt = note_pixels[0]
                    page.mouse.move(pt['x'], pt['y'])
                    time.sleep(1.0)
                    tooltip = page.locator('.hoverlayer .hovertext')
                    text = tooltip.first.evaluate('el => el.textContent') if tooltip.count() else '(none)'
                    page.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_soc_hover.png'),
                        clip={
                            'x': box['x'],
                            'y': box['y'],
                            'width': box['width'],
                            'height': int(box['height'] * 0.52),
                        },
                    )
                    print(f'  dashboard_soc_hover.png (tooltip: {text[:60]!r})')
                else:
                    # Fallback — best effort hover near first session shading
                    page.mouse.move(
                        box['x'] + box['width'] * 0.14,
                        box['y'] + box['height'] * 0.05,
                    )
                    time.sleep(0.8)
                    page.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_soc_hover.png'),
                        clip={
                            'x': box['x'],
                            'y': box['y'],
                            'width': box['width'],
                            'height': int(box['height'] * 0.52),
                        },
                    )
                    print('  dashboard_soc_hover.png (best effort — no note points found)')

            browser.close()

    finally:
        server.terminate()
        server.wait()
        print('\nServer stopped.')

    print(f'\nDone. Images written to {IMAGES_DIR}')


if __name__ == '__main__':
    take_missing()
