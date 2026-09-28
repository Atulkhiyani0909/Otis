from googleapiclient.discovery import build
from langchain_core.tools import tool
from tools.auth_helper import build_google_credentials


def get_tasks_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    return build("tasks", "v1", credentials=creds, static_discovery=False)


@tool
def list_tasks(max_results: int = 10) -> str:
    """Lists current pending tasks from the user's default Google Tasks list."""
    from agent import get_current_google_tokens

    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_tasks_service(tokens)
        results = (
            service.tasks()
            .list(
                tasklist="@default",
                maxResults=max_results,
                showCompleted=False,
                showHidden=False,
            )
            .execute()
        )
        items = results.get("items", [])

        if not items:
            return "You have no pending tasks in your checklist."

        output = []
        for item in items:
            title = item.get("title", "Untitled task")
            task_id = item.get("id")
            due = item.get("due", "No due date")
            output.append(f"• [{task_id}] {title} (Due: {due})")

        return "\n".join(output)
    except Exception as e:
        return f"Failed to fetch tasks: {str(e)}"


@tool
def create_task(title: str, notes: str = "", due_date_rfc3339: str = "") -> str:
    """
    Creates a new task in the user's default Google Tasks list.
    Args:
        title: Title/name of the task to complete.
        notes: Optional additional context, description, or sub-bullets.
        due_date_rfc3339: Optional RFC 3339 timestamp (e.g., '2026-09-28T18:00:00.000Z').
    """
    from agent import get_current_google_tokens

    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_tasks_service(tokens)
        task_body = {"title": title}
        if notes:
            task_body["notes"] = notes
        if due_date_rfc3339:
            task_body["due"] = due_date_rfc3339

        created = (
            service.tasks()
            .insert(tasklist="@default", body=task_body)
            .execute()
        )
        return f"Task created successfully: '{created.get('title')}' (ID: {created.get('id')})."
    except Exception as e:
        return f"Failed to create task: {str(e)}"


@tool
def complete_task(task_id: str) -> str:
    """
    Marks a task as completed in the user's default Google Tasks list.
    Args:
        task_id: The unique task ID retrieved from listing tasks.
    """
    from agent import get_current_google_tokens

    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_tasks_service(tokens)
        task = service.tasks().get(tasklist="@default", task=task_id).execute()
        task["status"] = "completed"

        service.tasks().update(
            tasklist="@default", task=task_id, body=task
        ).execute()
        return f"Marked task '{task.get('title', task_id)}' as completed."
    except Exception as e:
        return f"Failed to complete task: {str(e)}"


@tool
def delete_task(task_id: str) -> str:
    """
    Permanently deletes a task from the user's default Google Tasks list.
    Args:
        task_id: The unique task ID retrieved from listing tasks.
    """
    from agent import get_current_google_tokens

    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_tasks_service(tokens)
        # Fetch task metadata first so we can return its title in the response
        try:
            task = service.tasks().get(tasklist="@default", task=task_id).execute()
            task_title = task.get("title", task_id)
        except Exception:
            task_title = task_id

        # Permanently remove task from Google Tasks
        service.tasks().delete(tasklist="@default", task=task_id).execute()
        return f"Successfully deleted task: '{task_title}' (ID: {task_id})."
    except Exception as e:
        return f"Failed to delete task: {str(e)}"