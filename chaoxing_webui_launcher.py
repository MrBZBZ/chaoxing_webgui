#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller entrypoint for chaoxing_webui.

Keeps webui_defaults.json next to the executable so the packaged app can be
reused without rebuilding when the cookie or course URL changes.
"""

import os
import sys

import chaoxing_webui


def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def main():
    args = list(sys.argv[1:])
    if '--defaults' not in args:
        args = ['--defaults', os.path.join(_app_dir(), 'webui_defaults.json')] + args
    if '--open-browser' not in args:
        args.append('--open-browser')
    chaoxing_webui.main(args)


if __name__ == '__main__':
    main()
