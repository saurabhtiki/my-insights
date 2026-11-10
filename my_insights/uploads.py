"""An upload handler that keeps files in memory only (Django's default writes big uploads to
a temp file on disk, which "no data at rest" forbids). Too-big files are skipped and named."""

import io

from django.core.files.uploadhandler import MemoryFileUploadHandler, SkipFile


class MemoryOnlyUploadHandler(MemoryFileUploadHandler):
    def __init__(self, request, max_file_bytes: int, max_files: int):
        super().__init__(request)
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.files_seen = 0
        self.too_big: list[str] = []
        self.too_many: list[str] = []
        self.received = 0

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = True  # always memory, whatever the request size

    def new_file(self, field_name, file_name, *args, **kwargs):
        self.files_seen += 1
        if self.files_seen > self.max_files:
            self.too_many.append(file_name)
            self._drop_current_file()
            raise SkipFile()
        self.received = 0
        super().new_file(field_name, file_name, *args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.max_file_bytes:
            self.too_big.append(self.file_name)
            self._drop_current_file()
            raise SkipFile()
        return super().receive_data_chunk(raw_data, start)

    def _drop_current_file(self):
        """Django closes `self.file` when a file is skipped: give it an empty one to close,
        so what was read so far is freed and earlier files stay open."""
        self.file = io.BytesIO()
