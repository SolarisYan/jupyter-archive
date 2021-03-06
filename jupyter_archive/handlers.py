import os
import asyncio
import zipfile
import tarfile
import pathlib

from tornado import gen, web, iostream, ioloop
from notebook.base.handlers import IPythonHandler
from notebook.utils import url2path


# The delay in ms at which we send the chunk of data
# to the client.
ARCHIVE_DOWNLOAD_FLUSH_DELAY = 100
SUPPORTED_FORMAT = [
    "zip",
    "tgz",
    "tar.gz",
    "tbz",
    "tbz2",
    "tar.bz",
    "tar.bz2",
    "txz",
    "tar.xz",
]


class ArchiveStream:
    def __init__(self, handler):
        self.handler = handler
        self.position = 0

    def write(self, data):
        self.position += len(data)
        self.handler.write(data)
        del data

    def tell(self):
        return self.position

    def flush(self):
        # Note: Flushing is done elsewhere, in the main thread
        # because `write()` is called in a background thread.
        # self.handler.flush()
        pass


def make_writer(handler, archive_format="zip"):
    fileobj = ArchiveStream(handler)

    if archive_format == "zip":
        archive_file = zipfile.ZipFile(fileobj, mode="w", compression=zipfile.ZIP_DEFLATED)
        archive_file.add = archive_file.write
    elif archive_format in ["tgz", "tar.gz"]:
        archive_file = tarfile.open(fileobj=fileobj, mode="w|gz")
    elif archive_format in ["tbz", "tbz2", "tar.bz", "tar.bz2"]:
        archive_file = tarfile.open(fileobj=fileobj, mode="w|bz2")
    elif archive_format in ["txz", "tar.xz"]:
        archive_file = tarfile.open(fileobj=fileobj, mode="w|xz")
    else:
        raise ValueError("'{}' is not a valid archive format.".format(archive_format))
    return archive_file


def make_reader(archive_path):

    archive_format = "".join(archive_path.suffixes)[1:]

    if archive_format == "zip":
        archive_file = zipfile.ZipFile(archive_path, mode="r")
    elif archive_format in ["tgz", "tar.gz"]:
        archive_file = tarfile.open(archive_path, mode="r|gz")
    elif archive_format in ["tbz", "tbz2", "tar.bz", "tar.bz2"]:
        archive_file = tarfile.open(archive_path, mode="r|bz2")
    elif archive_format in ["txz", "tar.xz"]:
        archive_file = tarfile.open(archive_path, mode="r|xz")
    else:
        raise ValueError("'{}' is not a valid archive format.".format(archive_format))
    return archive_file


class DownloadArchiveHandler(IPythonHandler):
    @web.authenticated
    @gen.coroutine
    def get(self, archive_path, include_body=False):

        # /directories/ requests must originate from the same site
        self.check_xsrf_cookie()
        cm = self.contents_manager

        if cm.is_hidden(archive_path) and not cm.allow_hidden:
            self.log.info("Refusing to serve hidden file, via 404 Error")
            raise web.HTTPError(404)

        archive_token = self.get_argument("archiveToken")
        archive_format = self.get_argument("archiveFormat", "zip")
        if archive_format not in SUPPORTED_FORMAT:
            self.log.error("Unsupported format {}.".format(archive_format))
            raise web.HTTPError(404)

        archive_path = os.path.join(cm.root_dir, url2path(archive_path))

        archive_path = pathlib.Path(archive_path)
        archive_name = archive_path.name
        archive_filename = archive_path.with_suffix(".{}".format(archive_format)).name

        self.log.info("Prepare {} for archiving and downloading.".format(archive_filename))
        self.set_header("content-type", "application/octet-stream")
        self.set_header("cache-control", "no-cache")
        self.set_header("content-disposition", "attachment; filename={}".format(archive_filename))

        self.canceled = False
        self.flush_cb = ioloop.PeriodicCallback(self.flush, ARCHIVE_DOWNLOAD_FLUSH_DELAY)
        self.flush_cb.start()

        args = (archive_path, archive_format, archive_token)
        yield ioloop.IOLoop.current().run_in_executor(None, self.archive_and_download, *args)

        if self.canceled:
            self.log.info("Download canceled.")
        else:
            self.flush()
            self.log.info("Finished downloading {}.".format(archive_filename))

        self.set_cookie("archiveToken", archive_token)
        self.flush_cb.stop()
        self.finish()

    def archive_and_download(self, archive_path, archive_format, archive_token):

        with make_writer(self, archive_format) as archive:
            prefix = len(str(archive_path.parent)) + len(os.path.sep)
            for root, _, files in os.walk(archive_path):
                for file_ in files:
                    file_name = os.path.join(root, file_)
                    if not self.canceled:
                        self.log.debug("{}\n".format(file_name))
                        archive.add(file_name, os.path.join(root[prefix:], file_))
                    else:
                        break

    def on_connection_close(self):
        super().on_connection_close()
        self.canceled = True
        self.flush_cb.stop()


class ExtractArchiveHandler(IPythonHandler):
    @web.authenticated
    @gen.coroutine
    def get(self, archive_path, include_body=False):

        # /extract-archive/ requests must originate from the same site
        self.check_xsrf_cookie()
        cm = self.contents_manager

        if cm.is_hidden(archive_path) and not cm.allow_hidden:
            self.log.info("Refusing to serve hidden file, via 404 Error")
            raise web.HTTPError(404)

        archive_path = os.path.join(cm.root_dir, url2path(archive_path))
        archive_path = pathlib.Path(archive_path)

        yield ioloop.IOLoop.current().run_in_executor(None, self.extract_archive, archive_path)

        self.finish()

    def extract_archive(self, archive_path):

        archive_destination = archive_path.parent
        self.log.info("Begin extraction of {} to {}.".format(archive_path, archive_destination))

        archive_reader = make_reader(archive_path)
        with archive_reader as archive:
            archive.extractall(archive_destination)

        self.log.info("Finished extracting {} to {}.".format(archive_path, archive_destination))
