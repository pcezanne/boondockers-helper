#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
take_screenshots.py — Generate screenshots for the user manual.

Usage:
    python3 docs/take_screenshots.py [path/to/report.html]

If no report is given, uses the most recent file in reports/.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

IMAGES_DIR = Path(__file__).parent / 'images'
REPORTS_DIR = Path(__file__).parent.parent / 'reports'

VIEWPORT = {'width': 1400, 'height': 900}


def latest_report():
    reports = sorted(REPORTS_DIR.glob('*.html'))
    if not reports:
        raise FileNotFoundError(f'No HTML reports found in {REPORTS_DIR}')
    return reports[-1]


def take_screenshots(report_path: Path):
    IMAGES_DIR.mkdir(exist_ok=True)
    url = report_path.as_uri()
    print(f'Using report: {report_path.name}')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(url, wait_until='networkidle')

        # Give Plotly time to fully render all charts
        time.sleep(3)

        # ----------------------------------------------------------------
        # 1. Full report — wide scroll capture
        # ----------------------------------------------------------------
        page.set_viewport_size({'width': 1400, 'height': 900})
        page.evaluate('window.scrollTo(0, 0)')
        time.sleep(0.5)
        page.screenshot(
            path=str(IMAGES_DIR / 'full_report.png'),
            full_page=True,
        )
        print('  full_report.png')

        # ----------------------------------------------------------------
        # 2. Summary cards — all rows (group-label + summary divs)
        # ----------------------------------------------------------------
        card_els = page.query_selector_all('.group-label, .summary')
        if card_els:
            boxes = [el.bounding_box() for el in card_els]
            boxes = [b for b in boxes if b]
            if boxes:
                x = min(b['x'] for b in boxes)
                y = min(b['y'] for b in boxes)
                right = max(b['x'] + b['width'] for b in boxes)
                bottom = max(b['y'] + b['height'] for b in boxes)
                page.screenshot(
                    path=str(IMAGES_DIR / 'summary_cards.png'),
                    clip={'x': x - 10, 'y': y - 10,
                          'width': right - x + 20, 'height': bottom - y + 20},
                )
                print('  summary_cards.png')

        # ----------------------------------------------------------------
        # 3. Plotly chart (SOC + daily usage + charge rate all in one div)
        #    Capture the chart div, then crop sub-regions by scrolling
        # ----------------------------------------------------------------
        chart_div = page.query_selector('.plotly-graph-div')
        if chart_div:
            # Full chart
            chart_div.screenshot(path=str(IMAGES_DIR / 'chart_full.png'))
            print('  chart_full.png')

            # SOC region: top portion of the chart
            box = chart_div.bounding_box()
            if box:
                # SOC chart is roughly the top 45% of the figure
                soc_height = int(box['height'] * 0.45)
                page.screenshot(
                    path=str(IMAGES_DIR / 'chart_soc.png'),
                    clip={
                        'x': box['x'],
                        'y': box['y'],
                        'width': box['width'],
                        'height': soc_height,
                    },
                )
                print('  chart_soc.png')

                # Daily usage bar chart: roughly 45–65% down
                usage_y = box['y'] + box['height'] * 0.45
                usage_height = int(box['height'] * 0.20)
                page.screenshot(
                    path=str(IMAGES_DIR / 'chart_daily_usage.png'),
                    clip={
                        'x': box['x'],
                        'y': usage_y,
                        'width': box['width'],
                        'height': usage_height,
                    },
                )
                print('  chart_daily_usage.png')

                # Charge rate: roughly 65–100% down
                charge_y = box['y'] + box['height'] * 0.65
                charge_height = int(box['height'] * 0.35)
                page.screenshot(
                    path=str(IMAGES_DIR / 'chart_charge_rate.png'),
                    clip={
                        'x': box['x'],
                        'y': charge_y,
                        'width': box['width'],
                        'height': charge_height,
                    },
                )
                print('  chart_charge_rate.png')

        # ----------------------------------------------------------------
        # 4 & 5. Session tables — screenshot each table element directly
        # ----------------------------------------------------------------
        tables = page.query_selector_all('table')
        for i, tbl in enumerate(tables):
            tbl.scroll_into_view_if_needed()
            time.sleep(0.3)
            name = 'table_discharge.png' if i == 0 else 'table_charging.png'
            tbl.screenshot(path=str(IMAGES_DIR / name))
            print(f'  {name}')

        browser.close()

    print(f'\nDone. Images written to {IMAGES_DIR}')


if __name__ == '__main__':
    report = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_report()
    take_screenshots(report)
