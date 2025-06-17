# user-auth
For User Authentication

Before pushing the code, format it locally 

```
black . -l 120
```

For local dev
```
uvicorn app_layer.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug
celery -A celery_app worker --loglevel=info -P solo
```
[README.md](README.md)
For production

```
uvicorn main:app --host 0.0.0.0 --port 8000 --workers $(nproc) --reload

```
