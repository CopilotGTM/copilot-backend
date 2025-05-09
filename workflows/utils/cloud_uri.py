from urllib.parse import urlparse, unquote


def to_s3_uri(url):
    p = urlparse(url)
    bucket = p.hostname.split(".")[0]
    key = unquote(p.path.lstrip("/"))
    return f"s3://{bucket}/{key}"
