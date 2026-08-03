# Testing Strategy

This directory contains the automated tests for the application. We use the `pytest` framework to write and run our tests.

## Philosophy

Our testing strategy is based on the "Test Pyramid" concept, focusing primarily on integration tests for a balance of speed, reliability, and coverage.

-   **Unit Tests**: Should be used for testing small, isolated pieces of business logic, especially complex algorithms or utility functions. They should not involve database or network access.
-   **Integration Tests**: This is our primary focus. These tests verify the interaction between different components of our application, such as API endpoints, services, and the database. They are designed to test a complete feature flow from the API boundary down to the database.
-   **End-to-End (E2E) Tests**: These are larger-scale tests that run against a fully deployed application, including live external services. They are not included in this test suite and should be managed separately.

Deployed E2E tests live in `e2e/`. Follow
[`docs/Technical Documentation/testing/deployed-end-to-end-testing-framework.md`](../docs/Technical%20Documentation/testing/deployed-end-to-end-testing-framework.md)
when adding them; the framework deliberately favors a few durable business-flow
scenarios over many mocked unit tests.

## Key Features of Our Test Setup

-   **`pytest`**: The test runner and framework. Tests are simple Python functions.
-   **Test Database**: Tests run against a completely isolated, in-memory SQLite database. This is configured in `tests/conftest.py` and ensures that tests are repeatable and do not interfere with development data.
-   **`TestClient`**: We use FastAPI's `TestClient` (based on `httpx`) to make requests to our application in-memory, without needing to run a live web server.
-   **Dependency Injection**: We heavily leverage FastAPI's dependency injection system to override dependencies during testing. The most important example is replacing the database session (`get_org_session`) with our test database session.
-   **Mocking**: We use `pytest-mock` (which wraps `unittest.mock`) to isolate our application from external services (e.g., Azure Blob Storage, Celery). This makes our tests faster, more reliable, and avoids costs associated with real API calls.

## Running the Tests

1.  **Install Dependencies**: Make sure you have installed the development dependencies.

    ```bash
    pip install -r requirements_dev.txt
    ```

2.  **Run the Suite**: From the root of the project, simply run:

    ```bash
    pytest
    ```

    `pytest` will automatically discover all files named `test_*.py` or `*_test.py` and run the test functions within them.
