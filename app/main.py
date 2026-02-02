"""
FastAPI application that executes tasks using the host Claude binary.
"""
import subprocess
import json
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Claude Task Executor", version="1.0.0")


class TaskRequest(BaseModel):
    """Request model for Claude task execution."""
    prompt: str
    claude_binary_path: str = "/usr/local/bin/claude"
    timeout: Optional[int] = 300


class TaskResponse(BaseModel):
    """Response model for task execution."""
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "healthy", "service": "claude-task-executor"}


@app.get("/health")
async def health_check():
    """Detailed health check."""
    try:
        result = subprocess.run(
            ["/usr/local/bin/claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        claude_available = result.returncode == 0
        claude_version = result.stdout.strip() if claude_available else None
    except Exception as e:
        claude_available = False
        claude_version = str(e)

    return {
        "status": "healthy",
        "claude_available": claude_available,
        "claude_version": claude_version
    }


@app.post("/execute", response_model=TaskResponse)
async def execute_task(task: TaskRequest):
    """
    Execute a task using the Claude binary from the host system.

    Args:
        task: TaskRequest containing the prompt and configuration

    Returns:
        TaskResponse with execution results
    """
    try:
        # Execute claude with the provided prompt
        result = subprocess.run(
            [task.claude_binary_path, "code", task.prompt],
            capture_output=True,
            text=True,
            timeout=task.timeout
        )

        if result.returncode == 0:
            return TaskResponse(
                success=True,
                output=result.stdout
            )
        else:
            return TaskResponse(
                success=False,
                error=f"Claude exited with code {result.returncode}: {result.stderr}"
            )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=408,
            detail=f"Task execution timed out after {task.timeout} seconds"
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Claude binary not found at {task.claude_binary_path}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
