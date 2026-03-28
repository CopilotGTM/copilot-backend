Feature: Chat Sessions
  As a CopilotGTM user
  I want to manage my chat sessions
  So that I can organize my conversations

  Background:
    Given a user "alice@acme.com" in organisation "Acme Corp"

  # ----- Creation -----

  Scenario: Create a new chat session
    When Alice creates a chat session titled "Q1 Deal Review"
    Then the session is created successfully
    And the session title is "Q1 Deal Review"

  Scenario: Empty title is rejected
    When Alice creates a chat session with an empty title
    Then the request is rejected with "Title cannot be null or empty"

  Scenario: Long title is automatically truncated
    When Alice creates a chat session titled "This is an extremely long title that exceeds the maximum allowed character limit for a session title in our system and should be truncated"
    Then the session is created successfully
    And the session title is at most 100 characters
    And the session title ends with "..."

  # ----- Deletion -----

  Scenario: Delete a chat session
    Given Alice has a chat session titled "Old Chat"
    When Alice deletes the session
    Then the session is deleted successfully

  Scenario: Cannot delete another user's session
    Given a user "bob@acme.com" in organisation "Acme Corp"
    And Bob has a chat session titled "Bob's Private Chat"
    When Alice tries to delete Bob's session
    Then the request is rejected with "Chat session not found"
    And Bob's session still exists

  Scenario: Cannot delete a session from another organisation
    Given a user "carol@other.com" in organisation "Other Corp"
    And Carol has a chat session titled "Carol's Chat"
    When Alice tries to delete Carol's session
    Then the request is rejected with "Chat session not found"

  # ----- Rename -----

  Scenario: Rename a chat session
    Given Alice has a chat session titled "Draft"
    When Alice renames the session to "Final Review"
    Then the session title is "Final Review"

  Scenario: Cannot rename another user's session
    Given a user "bob@acme.com" in organisation "Acme Corp"
    And Bob has a chat session titled "Bob's Chat"
    When Alice tries to rename Bob's session to "Hacked"
    Then the request is rejected with "Chat session not found"
    And Bob's session title is still "Bob's Chat"

  # ----- Listing -----

  Scenario: User only sees their own sessions
    Given Alice has a chat session titled "Alice Chat 1"
    And Alice has a chat session titled "Alice Chat 2"
    And a user "bob@acme.com" in organisation "Acme Corp"
    And Bob has a chat session titled "Bob Chat 1"
    When Alice lists her sessions
    Then Alice sees 2 sessions
    And Alice does not see "Bob Chat 1"

  # ----- Favorites -----

  Scenario: Toggle session as favorite
    Given Alice has a chat session titled "Important Deal"
    When Alice marks the session as favorite
    Then the session is marked as favorite
    When Alice unmarks the session as favorite
    Then the session is not marked as favorite

  # ----- Search -----

  Scenario: Search sessions by title
    Given Alice has a chat session titled "Q1 Revenue Review"
    And Alice has a chat session titled "Q2 Pipeline Check"
    And Alice has a chat session titled "Team Standup Notes"
    When Alice searches for "Q1"
    Then Alice sees 1 session in results
    And the result contains "Q1 Revenue Review"
