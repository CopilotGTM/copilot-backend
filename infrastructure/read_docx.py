import fsspec
import docx

def open_docx(path: str) -> docx.Document:
    """
    Works for:
      - Local files:     "/foo/bar.docx"
      - S3:              "s3://my-bucket/path/to/file.docx"
      - Azure Blobs:     "abfs://container@account.dfs.core.windows.net/path.docx"
                         or "az://container/path.docx" (adlfs >= 2022.5.0)
    """
    # fsspec will look at the “scheme” and pick the right backend
    with fsspec.open(path, "rb") as fp:
        return docx.Document(fp)