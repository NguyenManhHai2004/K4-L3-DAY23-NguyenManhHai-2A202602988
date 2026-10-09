"""Windows mac dinh doc text bang cp1252 -> pytest khong doc duoc report tieng Viet (UTF-8).
Ep Path.read_text()/write_text() mac dinh UTF-8 trong luc chay test."""
import pathlib

_read, _write = pathlib.Path.read_text, pathlib.Path.write_text


def _r(self, encoding=None, errors=None):
    return _read(self, encoding=encoding or "utf-8", errors=errors)


def _w(self, data, encoding=None, errors=None, newline=None):
    return _write(self, data, encoding=encoding or "utf-8", errors=errors, newline=newline)


pathlib.Path.read_text, pathlib.Path.write_text = _r, _w
