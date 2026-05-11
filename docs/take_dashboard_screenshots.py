#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
take_dashboard_screenshots.py — Screenshot interactive dashboard elements.

Starts the Dash server, waits for it to be ready, then captures:
  - discharge table with Shore Power checkbox
  - discharge table with Shore Power checked (state temporarily toggled)
  - charging table with Type dropdown open
  - note textarea being edited
  - SOC chart hover tooltip over a noted session

Usage:
    python3 docs/take_dashboard_screenshots.py
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


def take_dashboard_screenshots():
    IMAGES_DIR.mkdir(exist_ok=True)

    # Start the Dash server
    server = subprocess.Popen(
        [sys.executable, '-m', 'victron.app', '--no-open'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print('Starting dashboard server...')
    time.sleep(4)  # give Dash time to bind and build initial layout

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1400, 'height': 900})

            if not wait_for_server(page):
                print('ERROR: Server did not start in time')
                return

            # Wait for the Dash chart element and summary cards to render
            page.wait_for_selector('#main-chart', timeout=30000)
            page.wait_for_selector('#summary-cards', timeout=30000)
            time.sleep(3)

            # ----------------------------------------------------------------
            # 1. Discharge table with Shore Power checkbox column visible
            # ----------------------------------------------------------------
            discharge_heading = page.locator('h2', has_text='Discharge Sessions')
            discharge_heading.scroll_into_view_if_needed()
            time.sleep(0.3)
            discharge_table = page.locator('#discharge-table table')
            if discharge_table.count():
                discharge_table.screenshot(
                    path=str(IMAGES_DIR / 'dashboard_discharge_table.png')
                )
                print('  dashboard_discharge_table.png')

            # ----------------------------------------------------------------
            # 2. Discharge table with Shore Power checked on first unchecked row
            #    — check it, screenshot, then uncheck to restore state
            # ----------------------------------------------------------------
            checkboxes = page.locator('input[type="checkbox"]')
            first_unchecked = None
            for i in range(checkboxes.count()):
                cb = checkboxes.nth(i)
                if not cb.is_checked():
                    first_unchecked = cb
                    break

            if first_unchecked:
                first_unchecked.check()
                time.sleep(1)  # allow summary cards to update
                discharge_table = page.locator('#discharge-table table')
                if discharge_table.count():
                    discharge_table.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_discharge_shore_checked.png')
                    )
                    print('  dashboard_discharge_shore_checked.png')
                # Restore
                first_unchecked.uncheck()
                time.sleep(1)

            # ----------------------------------------------------------------
            # 3. Charging table with Type dropdown open on first row
            # ----------------------------------------------------------------
            charging_heading = page.locator('h2', has_text='Charging Sessions')
            charging_heading.scroll_into_view_if_needed()
            time.sleep(0.3)

            # Click the first Type dropdown to open it
            dropdowns = page.locator('.Select-control')
            if dropdowns.count():
                dropdowns.first.click()
                time.sleep(0.5)
                charging_table = page.locator('#charging-table table')
                if charging_table.count():
                    charging_table.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_charging_dropdown_open.png')
                    )
                    print('  dashboard_charging_dropdown_open.png')
                # Close dropdown
                page.keyboard.press('Escape')
                time.sleep(0.3)

            # ----------------------------------------------------------------
            # 4. Note textarea with text (just screenshot an existing note)
            # ----------------------------------------------------------------
            textareas = page.locator('textarea')
            noted = None
            for i in range(textareas.count()):
                ta = textareas.nth(i)
                if ta.input_value().strip():
                    noted = ta
                    break

            if noted:
                noted.scroll_into_view_if_needed()
                time.sleep(0.2)
                noted.click()
                time.sleep(0.3)
                # Get parent cell for context
                cell = page.evaluate_handle('el => el.closest("td")', noted.element_handle())
                cell_el = cell.as_element()
                if cell_el:
                    cell_el.screenshot(path=str(IMAGES_DIR / 'dashboard_note_textarea.png'))
                    print('  dashboard_note_textarea.png')
                page.keyboard.press('Escape')
            else:
                # No existing notes — screenshot any empty textarea
                if textareas.count():
                    ta = textareas.first
                    ta.scroll_into_view_if_needed()
                    ta.click()
                    ta.fill('Example note')
                    time.sleep(0.2)
                    cell = page.evaluate_handle('el => el.closest("td")', ta.element_handle())
                    cell_el = cell.as_element()
                    if cell_el:
                        cell_el.screenshot(path=str(IMAGES_DIR / 'dashboard_note_textarea.png'))
                        print('  dashboard_note_textarea.png')
                    ta.fill('')  # clear it
                    page.keyboard.press('Escape')

            # ----------------------------------------------------------------
            # 5. SOC chart hover tooltip over a noted session region
            #    Scroll back to top, hover over the chart area
            # ----------------------------------------------------------------
            page.evaluate('window.scrollTo(0, 0)')
            time.sleep(0.3)
            chart = page.locator('.plotly-graph-div').first
            if chart.count():
                box = chart.bounding_box()
                if box:
                    # Hover in the upper-left quarter of the chart where session
                    # shading typically is
                    hover_x = box['x'] + box['width'] * 0.20
                    hover_y = box['y'] + box['height'] * 0.12
                    page.mouse.move(hover_x, hover_y)
                    time.sleep(1.0)
                    page.screenshot(
                        path=str(IMAGES_DIR / 'dashboard_soc_hover.png'),
                        clip={
                            'x': box['x'],
                            'y': box['y'],
                            'width': box['width'],
                            'height': int(box['height'] * 0.50),
                        },
                    )
                    print('  dashboard_soc_hover.png')

            browser.close()

    finally:
        server.terminate()
        server.wait()
        print('\nServer stopped.')

    print(f'\nDone. Images written to {IMAGES_DIR}')


if __name__ == '__main__':
    take_dashboard_screenshots()
