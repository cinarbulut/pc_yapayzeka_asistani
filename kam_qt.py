"""Kam'i PySide6 arayüzüyle başlatır. Eski Tkinter arayüzü için: py -3.12 asistan.py"""
import sys

from ui.qt_app import calistir

if __name__ == "__main__":
    sys.exit(calistir())
