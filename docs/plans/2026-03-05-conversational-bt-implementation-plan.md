# Implementation Plan: Conversational BT Architecture

## Phase 1: Schema & Database (foundation) ✅
1. Add `await_input` and `save_memory` to ProcedureStep schema
2. Update parser to pass through new fields
3. Add `customer_memories` and `sessions` tables to database schema

## Phase 2: Conversational Pause Points (core fix) ✅
4. Update `_compile_multi_tool` to insert UserInputNode at pause points
5. Update `_compile_single_tool` to respect `await_input`
6. Add default pause logic based on step type and on_success presence (`_should_pause`)
7. Add skip-on-resume to ToolActionNode and LLMResponseNode
8. Add MemoryWriteNode to nodes.py

## Phase 3: Completion Guard & Session Persistence ✅
9. Add completion guard to BTRunner (don't re-tick completed trees)
10. Add `save_session()` / `load_session()` to BTRunner
11. Add session save after every `run()` call in main.py
12. Add session restore in main.py chat endpoint (pause & resume)

## Phase 4: Customer Memory ✅
13. Add `MemoryWriteNode` with DB persistence via `customer_memories` table
14. Add `load_memories()` to BTRunner — loads at session start
15. LLMResponseNode includes customer memories in LLM prompt context
16. Step compilers add MemoryWriteNode at workflow completion when `save_memory: true`

## Phase 5: YAML & Integration ✅
17. Update finegrained YAML with `await_input` and `save_memory` markers
18. Run tests — 169 passing
19. Documentation updated
