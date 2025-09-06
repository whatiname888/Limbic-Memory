"""Minimal temporary FastAPI backend for Limbic Memory testing.

Endpoints are stubs so that the initialization script (start.sh) can
install dependencies and a simple server can be started for manual testing.

This file is intentionally lightweight and WILL be replaced by the real
memory engine implementation later.
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
import time

app = FastAPI(title="Limbic Memory (Temporary Backend)", version="0.0.1-temp")


class HealthResponse(BaseModel):
    status: str
    time: float


class ActivateRequest(BaseModel):
    query: str
    top_k: int = 5


class MemoryChunk(BaseModel):
    id: str
    content: str
    score: float
    source: Optional[str] = None


class ActivateResponse(BaseModel):
    query: str
    results: List[MemoryChunk]


class WriteRequest(BaseModel):
    chunks: List[str]


class WriteResponse(BaseModel):
    written: int


@app.get("/healthz", response_model=HealthResponse, summary="Health check")
def healthz():
    return HealthResponse(status="ok", time=time.time())


@app.post("/memory/activate", response_model=ActivateResponse, summary="Stub activation retrieval")
def activate(req: ActivateRequest):
    # Placeholder: returns synthetic results for now
    fake = [
        MemoryChunk(id=f"stub-{i}", content=f"Stub memory chunk {i} for '{req.query}'", score=1.0 - i * 0.05)
        for i in range(req.top_k)
    ]
    return ActivateResponse(query=req.query, results=fake)


@app.post("/memory/write", response_model=WriteResponse, summary="Stub write endpoint")
def write(req: WriteRequest):
    # Placeholder simply counts chunks
    return WriteResponse(written=len(req.chunks))


@app.get("/memory/debug/all", summary="List synthetic in-memory store (empty)")
def debug_all():
    # No real store yet
    return {"data": [], "note": "No real store implemented yet"}


# Convenience root
@app.get("/")
def root():
    return {"service": "limbic-memory-temp", "endpoints": ["/healthz", "/memory/activate", "/memory/write"], "note": "Temporary backend stub"}
