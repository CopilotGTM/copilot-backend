# Enhanced Knowledge Upload Batch API Response

## Overview
The `/presign/batch` endpoint now provides detailed error information for individual files that fail during the presign process. This allows the frontend to display specific error messages for each file.

## Response Structure

### Enhanced File Response
Each file in the batch response now includes:

```json
{
  "source_id": "uuid",
  "filename": "document.pdf",
  "mimetype": "application/pdf", 
  "size_bytes": 1024,
  "upload_url": "https://..." or null,
  "blob_path": "knowledge_sources/...",
  "expires_in_seconds": 900,
  "status": "uploaded" or "failed",
  "error_message": "Detailed error description" or null,
  "success": true or false
}
```

### Example Response with Mixed Success/Failure

```json
{
  "success": true,
  "message": "Batch presign completed with 1 errors",
  "data": {
    "files": [
      {
        "source_id": "123e4567-e89b-12d3-a456-426614174000",
        "filename": "success.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 2048,
        "upload_url": "https://storage.example.com/upload?sig=...",
        "blob_path": "knowledge_sources/user123/source456/success.pdf",
        "expires_in_seconds": 900,
        "status": "uploaded",
        "error_message": null,
        "success": true
      },
      {
        "source_id": "00000000-0000-0000-0000-000000000000",
        "filename": "failed.pdf",
        "mimetype": "",
        "size_bytes": 0,
        "upload_url": null,
        "blob_path": "",
        "expires_in_seconds": 0,
        "status": "failed", 
        "error_message": "Failed to generate upload URL: Storage account not accessible",
        "success": false
      }
    ],
    "errors": [
      {
        "index": 1,
        "filename": "failed.pdf",
        "source_id": "00000000-0000-0000-0000-000000000000",
        "reason": "signing_failed",
        "error_message": "Failed to generate upload URL: Storage account not accessible"
      }
    ]
  }
}
```

## Frontend Integration

The frontend can now:

1. **Check file-level success**: Use the `success` field to determine if each file was processed successfully
2. **Display specific errors**: Show the `error_message` for failed files
3. **Handle partial success**: Process successful uploads while showing errors for failed ones
4. **Correlate errors**: Use the `source_id` or `index` to match files with their specific errors

## Error Types

Common error scenarios include:
- `signing_failed`: Unable to generate storage upload URL (storage issues)
- `db_error`: Failed to save source record to database
- Validation errors are caught earlier in the request pipeline

This enhancement ensures users get clear feedback about which specific files failed and why, improving the user experience significantly.
