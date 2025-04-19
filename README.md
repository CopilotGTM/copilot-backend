# user-auth
For User Authentication

Before pushing the code, format it locally 

```
black . -l 120
```

For local dev
```
uvicorn app_layer.main:app --host 0.0.0.0 --port 8000 --reload
```
[README.md](README.md)
For production

```
uvicorn main:app --host 0.0.0.0 --port 8000 --workers $(nproc) --reload

```
