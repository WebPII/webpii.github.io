#!/usr/bin/env python3
"""
Batch Review GUI for UI Reproduction Pipeline.

Features:
- Run batches of unprocessed images
- Toggle between original/final/annotated views
- Ask questions about App.jsx via Claude CLI
- Fix and rerender functionality via Claude CLI

Usage:
    python batch_review_gui.py
    # Opens browser at http://localhost:5050
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, send_file

SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent  # pii/
STATE_FILE = SCRIPT_DIR / ".test_workflow_state.json"
FLAGS_FILE = SCRIPT_DIR / ".review_flags.json"
QUEUE_FILE = SCRIPT_DIR / ".queue_state.json"
OUTPUT_DIR = SCRIPT_DIR / "output"

app = Flask(__name__)
BASE_DIR_FOR_JS = str(BASE_DIR)

# Track running batch jobs
batch_status = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current_image": None,
    "results": [],
    "errors": []
}

HTML_TEMPLATE = '''{% raw %}
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UI Reproduction Batch Review</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        .header {
            background: #16213e;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #0f3460;
        }
        .header h1 { font-size: 1.5rem; color: #e94560; }
        .batch-controls {
            display: flex;
            gap: 1rem;
            align-items: center;
        }
        .btn {
            background: #e94560;
            color: white;
            border: none;
            padding: 0.75rem 1.5rem;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.2s;
        }
        .btn:hover { background: #ff6b6b; transform: translateY(-1px); }
        .btn:disabled { background: #555; cursor: not-allowed; transform: none; }
        .btn.secondary { background: #0f3460; }
        .btn.secondary:hover { background: #1a4a7a; }

        .main-container {
            display: flex;
            height: calc(100vh - 70px);
        }

        /* Left sidebar - runs list */
        .sidebar {
            width: 300px;
            background: #16213e;
            border-right: 1px solid #0f3460;
            overflow-y: auto;
        }
        .sidebar-header {
            padding: 1rem;
            border-bottom: 1px solid #0f3460;
            font-weight: 600;
        }
        .run-item {
            padding: 0.75rem 1rem;
            border-bottom: 1px solid #0f3460;
            cursor: pointer;
            transition: background 0.2s;
        }
        .run-item:hover { background: #1a3a5c; }
        .run-item.active { background: #0f3460; border-left: 3px solid #e94560; }
        .run-item.flagged { border-left: 3px solid #ffc107; }
        .run-item.active.flagged { border-left: 3px solid #ffc107; }
        .run-item .company { font-weight: 600; color: #e94560; }
        .run-item .page-type { font-size: 0.85rem; color: #aaa; }
        .run-item .timestamp { font-size: 0.75rem; color: #666; }

        /* Center - image viewer */
        .viewer {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .view-controls {
            padding: 1rem;
            background: #16213e;
            border-bottom: 1px solid #0f3460;
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }
        .view-btn {
            background: #0f3460;
            color: #aaa;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85rem;
        }
        .view-btn.active { background: #e94560; color: white; }
        .view-btn:hover:not(.active) { background: #1a4a7a; color: white; }

        .image-container {
            flex: 1;
            overflow: auto;
            padding: 1rem;
            display: flex;
            justify-content: center;
            align-items: flex-start;
        }
        .image-container img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }

        /* Right panel - code & chat */
        .right-panel {
            width: 450px;
            background: #16213e;
            border-left: 1px solid #0f3460;
            display: flex;
            flex-direction: column;
        }
        .panel-tabs {
            display: flex;
            border-bottom: 1px solid #0f3460;
        }
        .panel-tab {
            flex: 1;
            padding: 0.75rem;
            text-align: center;
            cursor: pointer;
            background: transparent;
            border: none;
            color: #aaa;
            font-weight: 600;
        }
        .panel-tab.active { background: #0f3460; color: #e94560; }

        .panel-content {
            flex: 1;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .panel-content.hidden { display: none; }

        .code-view {
            flex: 1;
            overflow: auto;
            padding: 1rem;
        }
        .code-view pre {
            background: #0d1b2a;
            padding: 1rem;
            border-radius: 6px;
            overflow-x: auto;
            font-size: 0.8rem;
            line-height: 1.4;
        }

        .chat-container {
            flex: 1;
            display: flex;
            flex-direction: column;
        }
        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 1rem;
        }
        .chat-message {
            margin-bottom: 1rem;
            padding: 0.75rem;
            border-radius: 8px;
        }
        .chat-message.user { background: #0f3460; margin-left: 2rem; }
        .chat-message.assistant { background: #1a3a5c; margin-right: 2rem; }
        .chat-message pre {
            background: #0d1b2a;
            padding: 0.5rem;
            border-radius: 4px;
            margin-top: 0.5rem;
            overflow-x: auto;
            font-size: 0.8rem;
        }
        .chat-input-container {
            padding: 1rem;
            border-top: 1px solid #0f3460;
            display: flex;
            gap: 0.5rem;
        }
        .chat-input {
            flex: 1;
            padding: 0.75rem;
            border: 1px solid #0f3460;
            border-radius: 6px;
            background: #0d1b2a;
            color: #eee;
            font-size: 0.9rem;
        }
        .chat-input:focus { outline: none; border-color: #e94560; }

        /* Fix panel */
        .fix-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 1rem;
            gap: 1rem;
        }
        .fix-container textarea {
            flex: 1;
            padding: 1rem;
            border: 1px solid #0f3460;
            border-radius: 6px;
            background: #0d1b2a;
            color: #eee;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.85rem;
            resize: none;
        }
        .fix-container textarea:focus { outline: none; border-color: #e94560; }

        /* Batch progress */
        .batch-progress {
            background: #0f3460;
            padding: 1rem;
            margin: 1rem;
            border-radius: 8px;
        }
        .progress-bar {
            height: 8px;
            background: #1a1a2e;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 0.5rem;
        }
        .progress-fill {
            height: 100%;
            background: #e94560;
            transition: width 0.3s;
        }

        /* Status indicator */
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 0.5rem;
        }
        .status-dot.running { background: #ffc107; animation: pulse 1s infinite; }
        .status-dot.idle { background: #28a745; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* Loading spinner */
        .spinner {
            border: 3px solid #0f3460;
            border-top: 3px solid #e94560;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .empty-state {
            text-align: center;
            padding: 3rem;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>UI Reproduction Batch Review</h1>
        <div style="font-size: 0.75rem; color: #666;">
            Keys: j/k=runs, 1-4=views, q/w/e/g=panels, r=refresh, f=flag, x=delete, c=copy, ←/→=history, [/]=browse
        </div>
        <div v-if="totalReproductionCost > 0" style="font-size: 0.85rem; color: #e94560; font-weight: 600;">
            Total: ${{ totalReproductionCost.toFixed(2) }} ({{ Math.round(totalReproductionDuration / 60) }}min)
        </div>
        <div class="batch-controls">
            <span>
                <span class="status-dot" :class="batchStatus.running ? 'running' : 'idle'"></span>
                <span v-if="batchStatus.running">Processing {{ batchStatus.completed }}/{{ batchStatus.total }}</span>
                <span v-else>Idle</span>
            </span>
            <input type="number" v-model="batchSize" min="1" max="50" style="width: 60px; padding: 0.5rem; background: #0d1b2a; border: 1px solid #0f3460; color: #eee; border-radius: 4px;">
            <button class="btn" @click="runBatch" :disabled="batchStatus.running">
                Run Batch
            </button>
            <button class="btn secondary" @click="refreshRuns">Refresh</button>
        </div>
    </div>

    <div class="main-container">
        <!-- Left sidebar -->
        <div class="sidebar">
            <div class="sidebar-header" style="display: flex; justify-content: space-between; align-items: center;">
                <span>Runs ({{ filteredRuns.length }}<span v-if="showFlaggedOnly">/{{ runs.length }}</span>)</span>
                <div style="display: flex; gap: 0.5rem; align-items: center;">
                    <span v-if="flagCount > 0" style="color: #ffc107; font-size: 0.8rem;">⚑ {{ flagCount }}</span>
                    <button @click="showFlaggedOnly = !showFlaggedOnly"
                            class="view-btn"
                            :class="{ active: showFlaggedOnly }"
                            style="padding: 0.25rem 0.5rem; font-size: 0.75rem;">
                        {{ showFlaggedOnly ? 'All' : 'Flagged' }}
                    </button>
                </div>
            </div>
            <div v-if="runs.length === 0" class="empty-state">
                <p>No runs yet</p>
                <p style="font-size: 0.85rem; margin-top: 0.5rem;">Click "Run Batch" to start</p>
            </div>
            <div v-for="(run, idx) in filteredRuns" :key="run.output_dir"
                 class="run-item"
                 :class="{ active: runs[selectedRun]?.output_dir === run.output_dir, flagged: flags[run.output_dir] }"
                 @click="selectRun(idx, true)">
                <div class="company" style="display: flex; justify-content: space-between; align-items: center;">
                    <span>
                        <span v-if="flags[run.output_dir]" style="color: #ffc107; margin-right: 4px;">⚑</span>
                        {{ run.company }}
                    </span>
                    <div style="display: flex; gap: 4px; align-items: center;">
                        <span v-if="reproductionCosts[run.output_dir]?.cost" style="color: #4ade80; font-size: 0.7rem;">
                            ${{ reproductionCosts[run.output_dir].cost.toFixed(2) }}
                        </span>
                        <span v-if="versionCounts[run.output_dir]" style="background: #0f3460; padding: 2px 6px; border-radius: 10px; font-size: 0.7rem; color: #aaa;">
                            v{{ versionCounts[run.output_dir] }}
                        </span>
                    </div>
                </div>
                <div class="page-type">{{ run.page_type }} #{{ run.image_id }}</div>
                <div class="timestamp">{{ run.timestamp }}</div>
            </div>
        </div>

        <!-- Center viewer -->
        <div class="viewer">
            <div class="view-controls" v-if="selectedRun !== null">
                <button class="view-btn" :class="{ active: currentView === 'original' }" @click="currentView = 'original'">[1] Original</button>
                <button class="view-btn" :class="{ active: currentView === 'final' }" @click="currentView = 'final'">[2] Final</button>
                <button class="view-btn" :class="{ active: currentView === 'annotated' }" @click="currentView = 'annotated'">[3] Annotated</button>
                <button class="view-btn" :class="{ active: currentView === 'annotated_partial' }" @click="currentView = 'annotated_partial'">[4] Partial</button>
                <span style="flex: 1;"></span>
                <button class="btn" @click="refreshAndRerender" style="padding: 0.5rem 1rem; font-size: 0.85rem;">
                    [r] Refresh+Screenshot
                </button>
                <button class="btn" @click="toggleFlag" :style="{ padding: '0.5rem 1rem', fontSize: '0.85rem', background: currentRunFlagged ? '#ffc107' : '#6c757d', color: currentRunFlagged ? '#000' : '#fff' }">
                    [f] {{ currentRunFlagged ? 'Flagged' : 'Flag' }}
                </button>
                <button class="btn" @click="deleteRun" style="padding: 0.5rem 1rem; font-size: 0.85rem; background: #c23616;">
                    [x] Delete
                </button>
                <!-- Version selector -->
                <select v-if="versions.length > 0" @change="restoreVersion($event.target.value)" style="padding: 0.5rem; background: #0f3460; color: #aaa; border: 1px solid #1a4a7a; border-radius: 4px; font-size: 0.85rem;">
                    <option value="">History ({{ versions.length }})</option>
                    <option v-for="v in versions" :key="v.version_id" :value="v.version_id">
                        {{ formatVersionTime(v.timestamp) }} - {{ v.fix_prompt ? v.fix_prompt.slice(0, 30) + '...' : 'backup' }}
                    </option>
                </select>
            </div>

            <div class="image-container" v-if="selectedRun !== null">
                <img :src="getImageUrl(currentView)" />
            </div>

            <div v-else class="empty-state" style="flex: 1; display: flex; align-items: center; justify-content: center;">
                <div>
                    <p style="font-size: 1.2rem;">Select a run from the sidebar</p>
                    <p style="color: #666; margin-top: 0.5rem;">Or run a new batch to get started</p>
                </div>
            </div>

            <!-- Batch progress -->
            <div v-if="batchStatus.running" class="batch-progress">
                <div>Processing: {{ batchStatus.current_image || 'Starting...' }}</div>
                <div class="progress-bar">
                    <div class="progress-fill" :style="{ width: (batchStatus.completed / batchStatus.total * 100) + '%' }"></div>
                </div>
            </div>

            <!-- Operation queue -->
            <div v-if="rerenderProcessing || rerenderQueue.length > 0" class="batch-progress" style="background: #1a3a5c;">
                <div style="font-weight: 600; margin-bottom: 0.5rem;">
                    Queue ({{ rerenderQueue.length + (rerenderCurrent ? 1 : 0) }})
                </div>
                <div v-if="rerenderCurrent" style="color: #ffc107; font-size: 0.85rem;">
                    ▶ {{ rerenderCurrent.label }} <span style="color: #e94560;">[{{ rerenderCurrent.type }}]</span>
                </div>
                <div v-for="(item, idx) in rerenderQueue" :key="idx" style="color: #aaa; font-size: 0.8rem; padding-left: 1rem; display: flex; justify-content: space-between; align-items: center;">
                    <span>{{ idx + 1 }}. {{ item.label }} <span style="color: #888;">[{{ item.type }}]</span></span>
                    <span @click.stop="removeFromQueue(idx)" style="color: #ef4444; cursor: pointer; padding: 0 0.5rem; font-weight: bold;" title="Remove from queue">×</span>
                </div>
            </div>

            <!-- Completed history - click to jump, arrows to navigate -->
            <div v-if="currentCompletedItem"
                 class="batch-progress"
                 style="background: #1e4620; cursor: pointer;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
                    <span style="color: #4ade80; font-size: 0.75rem;">
                        Completed {{ completedHistoryIdx + 1 }}/{{ completedHistory.length }}
                    </span>
                    <div style="display: flex; gap: 0.5rem;">
                        <span @click.stop="prevCompleted" :style="{ color: completedHistoryIdx < completedHistory.length - 1 ? '#4ade80' : '#666', cursor: 'pointer' }">[←]</span>
                        <span @click.stop="nextCompleted" :style="{ color: completedHistoryIdx > 0 ? '#4ade80' : '#666', cursor: 'pointer' }">[→]</span>
                    </div>
                </div>
                <div @click="jumpToCompleted" style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <span style="color: #4ade80;">✓</span>
                        <span style="color: #aaa; font-size: 0.85rem; margin-left: 0.5rem;">{{ currentCompletedItem.label }}</span>
                        <span style="color: #4ade80; font-size: 0.75rem; margin-left: 0.5rem;">[{{ currentCompletedItem.type }}]</span>
                    </div>
                    <span style="color: #4ade80; font-size: 0.75rem;">Click to view</span>
                </div>
            </div>
        </div>

        <!-- Right panel -->
        <div class="right-panel" v-if="selectedRun !== null">
            <div class="panel-tabs">
                <button class="panel-tab" :class="{ active: activePanel === 'code' }" @click="activePanel = 'code'">Code</button>
                <button class="panel-tab" :class="{ active: activePanel === 'chat' }" @click="activePanel = 'chat'">Ask AI</button>
                <button class="panel-tab" :class="{ active: activePanel === 'fix' }" @click="activePanel = 'fix'">Fix</button>
            </div>

            <!-- Code panel -->
            <div class="panel-content" :class="{ hidden: activePanel !== 'code' }">
                <div class="code-view">
                    <h3 style="margin-bottom: 1rem;">App.jsx</h3>
                    <pre><code>{{ currentCode }}</code></pre>
                </div>
            </div>

            <!-- Chat panel -->
            <div class="panel-content chat-container" :class="{ hidden: activePanel !== 'chat' }">
                <div class="chat-messages" ref="chatMessages">
                    <div v-if="chatHistory.length === 0" class="empty-state">
                        <p>Ask questions about the code</p>
                        <p style="font-size: 0.85rem; margin-top: 0.5rem;">Powered by Claude</p>
                    </div>
                    <div v-for="(msg, idx) in chatHistory" :key="idx" class="chat-message" :class="msg.role">
                        <div v-html="formatMessage(msg.content)"></div>
                    </div>
                    <div v-if="chatLoading" class="chat-message assistant">
                        <div class="spinner"></div>
                    </div>
                </div>
                <div class="chat-input-container">
                    <input class="chat-input"
                           v-model="chatInput"
                           @keyup.enter="sendChat"
                           placeholder="Ask about this UI reproduction..."
                           :disabled="chatLoading">
                    <button class="btn" @click="sendChat" :disabled="chatLoading || !chatInput.trim()">Send</button>
                </div>
            </div>

            <!-- Fix panel -->
            <div class="panel-content fix-container" :class="{ hidden: activePanel !== 'fix' }">
                <div>
                    <label style="font-weight: 600;">Describe the fix needed:</label>
                </div>
                <textarea v-model="fixPrompt" placeholder="Describe what needs to be fixed in the UI reproduction..."></textarea>
                <button class="btn" @click="applyFix" :disabled="!fixPrompt.trim()">
                    Queue Fix & Rerender
                </button>
            </div>
        </div>
    </div>

    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <script>
        const { createApp, ref, computed, onMounted, watch, nextTick } = Vue;
        const baseDir = __BASE_DIR_JSON__;

        createApp({
            setup() {
                const runs = ref([]);
                const selectedRun = ref(null);
                const currentView = ref('original');
                const activePanel = ref('code');
                const batchSize = ref(10);
                const batchStatus = ref({ running: false, total: 0, completed: 0, current_image: null });

                const currentCode = ref('');
                const chatHistory = ref([]);
                const chatInput = ref('');
                const chatLoading = ref(false);
                const chatMessages = ref(null);

                const fixPrompt = ref('');

                // Flagging system
                const flags = ref({});  // { output_dir: true/false }
                const showFlaggedOnly = ref(false);

                const filteredRuns = computed(() => {
                    if (!showFlaggedOnly.value) return runs.value;
                    return runs.value.filter(run => flags.value[run.output_dir]);
                });

                const flagCount = computed(() => {
                    return Object.values(flags.value).filter(Boolean).length;
                });

                const currentRunFlagged = computed(() => {
                    if (selectedRun.value === null) return false;
                    const run = runs.value[selectedRun.value];
                    return run && flags.value[run.output_dir];
                });

                const loadFlags = async () => {
                    try {
                        const resp = await fetch('/api/flags');
                        const data = await resp.json();
                        flags.value = data.flags || {};
                    } catch (e) {
                        console.error('Failed to load flags:', e);
                    }
                };

                const toggleFlag = async () => {
                    if (selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];
                    if (!run) return;

                    const newValue = !flags.value[run.output_dir];
                    try {
                        await fetch('/api/flags', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ output_dir: run.output_dir, flagged: newValue })
                        });
                        flags.value[run.output_dir] = newValue;
                    } catch (e) {
                        console.error('Failed to toggle flag:', e);
                    }
                };

                // Version control
                const versions = ref([]);
                const versionCounts = ref({});

                // Reproduction costs
                const reproductionCosts = ref({});  // { output_dir: { cost, duration } }
                const totalReproductionCost = ref(0);
                const totalReproductionDuration = ref(0);

                const loadReproductionCosts = async () => {
                    try {
                        const resp = await fetch('/api/reproduction-costs');
                        const data = await resp.json();
                        reproductionCosts.value = data.costs || {};
                        totalReproductionCost.value = data.total_cost || 0;
                        totalReproductionDuration.value = data.total_duration || 0;
                    } catch (e) {
                        console.error('Failed to load reproduction costs:', e);
                    }
                };

                const copyFolderPath = () => {
                    if (selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];
                    if (!run) return;

                    const fullPath = `${baseDir}/${run.output_dir}`;
                    navigator.clipboard.writeText(fullPath).then(() => {
                        // Brief visual feedback - could add a toast here
                        console.log('Copied:', fullPath);
                    });
                };

                const loadVersions = async () => {
                    if (selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];
                    if (!run) return;

                    try {
                        const resp = await fetch(`/api/versions?output_dir=${encodeURIComponent(run.output_dir)}`);
                        const data = await resp.json();
                        versions.value = data.versions || [];
                    } catch (e) {
                        console.error('Failed to load versions:', e);
                        versions.value = [];
                    }
                };

                const loadVersionCounts = async () => {
                    try {
                        const resp = await fetch('/api/version-count');
                        const data = await resp.json();
                        versionCounts.value = data.counts || {};
                    } catch (e) {
                        console.error('Failed to load version counts:', e);
                    }
                };

                const restoreVersion = async (versionId) => {
                    if (!versionId || selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];

                    if (!confirm(`Restore to version ${versionId}? Current changes will be lost.`)) return;

                    try {
                        const resp = await fetch('/api/restore-version', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ output_dir: run.output_dir, version_id: versionId })
                        });
                        const data = await resp.json();
                        if (data.success) {
                            // Reload code and refresh images
                            imageRefreshKey.value = Date.now();
                            await selectRun(selectedRun.value);
                            alert('Version restored!');
                        } else {
                            alert('Error: ' + data.error);
                        }
                    } catch (e) {
                        alert('Error: ' + e.message);
                    }
                };

                const formatVersionTime = (timestamp) => {
                    if (!timestamp) return '';
                    try {
                        const d = new Date(timestamp);
                        return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                    } catch {
                        return timestamp.slice(0, 15);
                    }
                };

                // Chat history persistence
                const saveChatHistory = async () => {
                    if (selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];
                    if (!run) return;

                    try {
                        await fetch('/api/chat-history', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ output_dir: run.output_dir, history: chatHistory.value })
                        });
                    } catch (e) {
                        console.error('Failed to save chat history:', e);
                    }
                };

                const loadChatHistory = async () => {
                    if (selectedRun.value === null) return;
                    const run = runs.value[selectedRun.value];
                    if (!run) return;

                    try {
                        const resp = await fetch(`/api/chat-history?output_dir=${encodeURIComponent(run.output_dir)}`);
                        const data = await resp.json();
                        chatHistory.value = data.history || [];
                    } catch (e) {
                        console.error('Failed to load chat history:', e);
                        chatHistory.value = [];
                    }
                };

                // Re-screenshot queue system
                const rerenderQueue = ref([]);  // [{idx, output_dir, label}]
                const rerenderCurrent = ref(null);  // Currently processing item
                const rerenderProcessing = ref(false);
                const imageRefreshKey = ref(0);  // Increment to force image refresh

                // Completed history (most recent first)
                const completedHistory = ref([]);  // Array of completed items
                const completedHistoryIdx = ref(0);  // Current position (0 = most recent)
                const MAX_HISTORY = 20;

                const currentCompletedItem = computed(() => {
                    if (completedHistory.value.length === 0) return null;
                    return completedHistory.value[completedHistoryIdx.value] || null;
                });

                // Queue persistence
                const saveQueueState = async () => {
                    try {
                        await fetch('/api/queue', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                queue: rerenderQueue.value,
                                completedHistory: completedHistory.value
                            })
                        });
                    } catch (e) {
                        console.error('Failed to save queue state:', e);
                    }
                };

                const loadQueueState = async () => {
                    try {
                        const resp = await fetch('/api/queue');
                        const data = await resp.json();
                        if (data.queue && data.queue.length > 0) {
                            rerenderQueue.value = data.queue;
                            // Auto-resume processing
                            processRerenderQueue();
                        }
                        if (data.completedHistory && data.completedHistory.length > 0) {
                            completedHistory.value = data.completedHistory;
                        } else if (data.lastCompleted) {
                            // Migrate from old format
                            completedHistory.value = [data.lastCompleted];
                        }
                    } catch (e) {
                        console.error('Failed to load queue state:', e);
                    }
                };

                const addToCompletedHistory = (item) => {
                    completedHistory.value.unshift({ ...item, completedAt: Date.now() });
                    if (completedHistory.value.length > MAX_HISTORY) {
                        completedHistory.value = completedHistory.value.slice(0, MAX_HISTORY);
                    }
                    completedHistoryIdx.value = 0;  // Reset to most recent
                };

                const jumpToCompleted = () => {
                    const item = currentCompletedItem.value;
                    if (!item) return;
                    const idx = item.idx;
                    if (idx !== null && idx < runs.value.length) {
                        selectRun(idx);
                        nextTick(() => {
                            const items = document.querySelectorAll('.run-item');
                            if (items[idx]) {
                                items[idx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                            }
                        });
                    }
                };

                const prevCompleted = () => {
                    if (completedHistoryIdx.value < completedHistory.value.length - 1) {
                        completedHistoryIdx.value++;
                    }
                };

                const nextCompleted = () => {
                    if (completedHistoryIdx.value > 0) {
                        completedHistoryIdx.value--;
                    }
                };

                const processRerenderQueue = async () => {
                    if (rerenderProcessing.value || rerenderQueue.value.length === 0) return;

                    rerenderProcessing.value = true;

                    while (rerenderQueue.value.length > 0) {
                        // Remove from queue first, then set as current
                        const item = rerenderQueue.value.shift();
                        rerenderCurrent.value = item;
                        saveQueueState();  // Persist after removing from queue

                        try {
                            let endpoint, body;
                            if (item.type === 'fix') {
                                endpoint = '/api/fix';
                                body = { output_dir: item.output_dir, prompt: item.prompt, chat_history: chatHistory.value };
                            } else if (item.type === 'refresh') {
                                endpoint = '/api/refresh-and-rerender';
                                body = { output_dir: item.output_dir };
                            } else {
                                endpoint = '/api/rerender';
                                body = { output_dir: item.output_dir };
                            }

                            const resp = await fetch(endpoint, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body)
                            });
                            const data = await resp.json();
                            if (data.success) {
                                // Track completed for quick navigation
                                addToCompletedHistory(item);
                                saveQueueState();  // Persist after completion
                                // Force image refresh
                                imageRefreshKey.value = Date.now();
                                // Reload code if it was a fix
                                if (item.type === 'fix') {
                                    if (selectedRun.value === item.idx) {
                                        const run = runs.value[item.idx];
                                        const codeResp = await fetch(`/api/code?output_dir=${encodeURIComponent(run.output_dir)}`);
                                        const codeData = await codeResp.json();
                                        currentCode.value = codeData.code || '// Code not found';
                                    }
                                    // Show model output in chat panel - save to the CORRECT run, not current selection
                                    if (data.model_output) {
                                        const costInfo = data.cost_usd ? `\n\n*Cost: $${data.cost_usd.toFixed(4)}*` : '';
                                        const fixMessage = {
                                            role: 'assistant',
                                            content: `**Fix applied:** ${item.prompt}\n\n**Model output:**\n${data.model_output.slice(0, 2000)}${data.model_output.length > 2000 ? '...' : ''}${costInfo}`
                                        };

                                        // Save to the correct run's chat history via API
                                        const existingHistory = await fetch(`/api/chat-history?output_dir=${encodeURIComponent(item.output_dir)}`).then(r => r.json());
                                        const updatedHistory = [...(existingHistory.history || []), fixMessage];
                                        await fetch('/api/chat-history', {
                                            method: 'POST',
                                            headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ output_dir: item.output_dir, history: updatedHistory })
                                        });

                                        // Only update local state if viewing the same run
                                        if (selectedRun.value === item.idx) {
                                            chatHistory.value = updatedHistory;
                                            activePanel.value = 'chat';
                                        }
                                    }
                                    // Update version counts and reload versions
                                    loadVersionCounts();
                                    if (selectedRun.value === item.idx) {
                                        loadVersions();
                                    }
                                }
                            } else {
                                console.error('Operation failed:', item.label, data.error);
                                const errorMsg = data.model_output
                                    ? `${data.error}\n\nModel output:\n${data.model_output.slice(0, 1000)}`
                                    : data.error;
                                alert(`Error (${item.type}): ${errorMsg}`);
                            }
                        } catch (e) {
                            console.error('Operation error:', item.label, e);
                        }
                    }

                    rerenderCurrent.value = null;
                    rerenderProcessing.value = false;
                };

                const queueOperation = (idx, type = 'rerender', extra = {}) => {
                    if (idx === null || idx === undefined) return;
                    const run = runs.value[idx];
                    if (!run) return;

                    // Don't add duplicates (same output_dir and type)
                    if (rerenderQueue.value.some(q => q.output_dir === run.output_dir && q.type === type)) return;
                    if (rerenderCurrent.value && rerenderCurrent.value.output_dir === run.output_dir && rerenderCurrent.value.type === type) return;

                    rerenderQueue.value.push({
                        idx,
                        output_dir: run.output_dir,
                        label: `${run.company} - ${run.page_type} #${run.image_id}`,
                        type,  // 'rerender', 'refresh', or 'fix'
                        ...extra  // For fix: { prompt: '...' }
                    });

                    saveQueueState();  // Persist after adding

                    // Start processing if not already
                    processRerenderQueue();
                };

                // Backwards compatible wrapper
                const queueRerender = (idx, refresh = false) => {
                    queueOperation(idx, refresh ? 'refresh' : 'rerender');
                };

                const removeFromQueue = (idx) => {
                    rerenderQueue.value.splice(idx, 1);
                    saveQueueState();  // Persist after removing
                };

                const fetchRuns = async () => {
                    const resp = await fetch('/api/runs');
                    const data = await resp.json();
                    runs.value = data.runs;
                };

                const selectRun = async (idx, fromFiltered = false) => {
                    // If selecting from filtered list, find the actual index in runs.value
                    let actualIdx = idx;
                    if (fromFiltered && showFlaggedOnly.value) {
                        const filteredRun = filteredRuns.value[idx];
                        actualIdx = runs.value.findIndex(r => r.output_dir === filteredRun.output_dir);
                    }
                    selectedRun.value = actualIdx;

                    // Load code
                    const run = runs.value[actualIdx];
                    const resp = await fetch(`/api/code?output_dir=${encodeURIComponent(run.output_dir)}`);
                    const data = await resp.json();
                    currentCode.value = data.code || '// Code not found';

                    // Load chat history and versions for this run
                    await loadChatHistory();
                    await loadVersions();
                };

                const getImageUrl = (type) => {
                    if (selectedRun.value === null) return '';
                    const run = runs.value[selectedRun.value];
                    // Include imageRefreshKey to force refresh after rerender
                    return `/api/image?output_dir=${encodeURIComponent(run.output_dir)}&type=${type}&t=${imageRefreshKey.value}`;
                };

                const runBatch = async () => {
                    batchStatus.value = { running: true, total: batchSize.value, completed: 0, current_image: null };

                    try {
                        const resp = await fetch('/api/batch/start', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ count: batchSize.value })
                        });

                        // Poll for status
                        const pollStatus = async () => {
                            const statusResp = await fetch('/api/batch/status');
                            const status = await statusResp.json();
                            batchStatus.value = status;

                            if (status.running) {
                                setTimeout(pollStatus, 2000);
                            } else {
                                await fetchRuns();
                            }
                        };

                        pollStatus();
                    } catch (e) {
                        console.error('Batch error:', e);
                        batchStatus.value.running = false;
                    }
                };

                const refreshRuns = () => fetchRuns();

                const formatMessage = (content) => {
                    // Basic markdown-like formatting
                    return content
                        .replace(/```(\\w*)\\n([\\s\\S]*?)```/g, '<pre><code>$2</code></pre>')
                        .replace(/`([^`]+)`/g, '<code style="background:#0d1b2a;padding:2px 4px;border-radius:3px;">$1</code>')
                        .replace(/\\n/g, '<br>');
                };

                const sendChat = async () => {
                    if (!chatInput.value.trim() || chatLoading.value) return;

                    const userMessage = chatInput.value.trim();
                    chatHistory.value.push({ role: 'user', content: userMessage });
                    chatInput.value = '';
                    chatLoading.value = true;

                    // Save immediately so user message persists even if request fails
                    saveChatHistory();

                    await nextTick();
                    if (chatMessages.value) {
                        chatMessages.value.scrollTop = chatMessages.value.scrollHeight;
                    }

                    try {
                        const run = runs.value[selectedRun.value];
                        const resp = await fetch('/api/chat', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                message: userMessage,
                                output_dir: run.output_dir,
                                history: chatHistory.value.slice(0, -1)  // Exclude the message we just added
                            })
                        });

                        const data = await resp.json();
                        const costInfo = data.cost_usd ? `\n\n*Cost: $${data.cost_usd.toFixed(4)}*` : '';
                        chatHistory.value.push({ role: 'assistant', content: data.response + costInfo });
                    } catch (e) {
                        chatHistory.value.push({ role: 'assistant', content: 'Error: ' + e.message });
                    } finally {
                        chatLoading.value = false;
                        await nextTick();
                        if (chatMessages.value) {
                            chatMessages.value.scrollTop = chatMessages.value.scrollHeight;
                        }
                        // Save again with assistant response
                        saveChatHistory();
                    }
                };

                const applyFix = () => {
                    if (!fixPrompt.value.trim() || selectedRun.value === null) return;

                    // Queue the fix operation
                    queueOperation(selectedRun.value, 'fix', { prompt: fixPrompt.value.trim() });

                    // Clear the prompt and switch to final view to watch result
                    fixPrompt.value = '';
                    currentView.value = 'final';
                };

                const rerunScreenshot = () => {
                    // Queue current selection for re-screenshot
                    queueRerender(selectedRun.value);
                };

                const refreshAndRerender = () => {
                    // Queue current selection for refresh + re-screenshot
                    queueRerender(selectedRun.value, true);
                };

                const deleteRun = async () => {
                    if (selectedRun.value === null) return;

                    const run = runs.value[selectedRun.value];
                    if (!confirm(`Delete this run?\n\n${run.company} - ${run.page_type} #${run.image_id}\n\nThis cannot be undone.`)) {
                        return;
                    }

                    try {
                        const resp = await fetch('/api/delete-run', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ output_dir: run.output_dir })
                        });
                        const data = await resp.json();
                        if (data.success) {
                            // Refresh runs list and select previous or next item
                            const prevIdx = selectedRun.value;
                            await fetchRuns();
                            // Select the next run (or previous if was last)
                            if (runs.value.length > 0) {
                                const newIdx = Math.min(prevIdx, runs.value.length - 1);
                                selectRun(newIdx);
                            } else {
                                selectedRun.value = null;
                            }
                        } else {
                            alert('Error deleting run: ' + data.error);
                        }
                    } catch (e) {
                        alert('Error: ' + e.message);
                    }
                };

                // Keyboard shortcuts
                const scrollToSelectedRun = (idx) => {
                    nextTick(() => {
                        const items = document.querySelectorAll('.run-item');
                        if (items[idx]) {
                            items[idx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                        }
                    });
                };

                const handleKeydown = (e) => {
                    // Skip if typing in input/textarea
                    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

                    // Navigation: j/k for runs (navigate within filtered list)
                    if (e.key === 'j') {
                        e.preventDefault();
                        const list = filteredRuns.value;
                        if (list.length > 0) {
                            // Find current position in filtered list
                            const currentRun = runs.value[selectedRun.value];
                            const currentFilteredIdx = currentRun ? list.findIndex(r => r.output_dir === currentRun.output_dir) : -1;
                            const nextFilteredIdx = currentFilteredIdx === -1 ? 0 : Math.min(currentFilteredIdx + 1, list.length - 1);
                            selectRun(nextFilteredIdx, true);
                            scrollToSelectedRun(nextFilteredIdx);
                        }
                    } else if (e.key === 'k') {
                        e.preventDefault();
                        const list = filteredRuns.value;
                        if (list.length > 0 && selectedRun.value !== null) {
                            const currentRun = runs.value[selectedRun.value];
                            const currentFilteredIdx = currentRun ? list.findIndex(r => r.output_dir === currentRun.output_dir) : 0;
                            const prevFilteredIdx = Math.max(currentFilteredIdx - 1, 0);
                            selectRun(prevFilteredIdx, true);
                            scrollToSelectedRun(prevFilteredIdx);
                        }
                    }
                    // Image views: 1-4
                    else if (e.key === '1') { currentView.value = 'original'; }
                    else if (e.key === '2') { currentView.value = 'final'; }
                    else if (e.key === '3') { currentView.value = 'annotated'; }
                    else if (e.key === '4') { currentView.value = 'annotated_partial'; }
                    // Panels: q/w/e/g
                    else if (e.key === 'q') { activePanel.value = 'code'; }
                    else if (e.key === 'w') { activePanel.value = 'chat'; }
                    else if (e.key === 'e' || e.key === 'g') { activePanel.value = 'fix'; }
                    // Refresh data + re-screenshot: r
                    else if (e.key === 'r') { refreshAndRerender(); }
                    // Flag: f
                    else if (e.key === 'f') { toggleFlag(); }
                    // Delete: x
                    else if (e.key === 'x') { deleteRun(); }
                    // Copy path: c
                    else if (e.key === 'c') { copyFolderPath(); }
                    // Completed history navigation: arrow keys, [ and ]
                    else if (e.key === 'ArrowRight') { jumpToCompleted(); }
                    else if (e.key === 'ArrowLeft') { prevCompleted(); }
                    else if (e.key === '[') { prevCompleted(); }
                    else if (e.key === ']') { nextCompleted(); }
                };

                onMounted(async () => {
                    await fetchRuns();
                    loadFlags();
                    loadVersionCounts();
                    loadReproductionCosts();
                    loadQueueState();  // Load and resume queue after runs are loaded
                    window.addEventListener('keydown', handleKeydown);
                });

                return {
                    runs, selectedRun, currentView, activePanel, batchSize, batchStatus,
                    currentCode, chatHistory, chatInput, chatLoading, chatMessages,
                    fixPrompt,
                    flags, showFlaggedOnly, filteredRuns, flagCount, currentRunFlagged, toggleFlag,
                    versions, versionCounts, restoreVersion, formatVersionTime,
                    reproductionCosts, totalReproductionCost, totalReproductionDuration, copyFolderPath,
                    rerenderQueue, rerenderCurrent, rerenderProcessing, queueRerender, queueOperation, removeFromQueue, imageRefreshKey,
                    completedHistory, completedHistoryIdx, currentCompletedItem, jumpToCompleted, prevCompleted, nextCompleted,
                    fetchRuns, selectRun, getImageUrl, runBatch, refreshRuns,
                    formatMessage, sendChat, applyFix, refreshAndRerender, deleteRun
                };
            }
        }).mount('body');
    </script>
</body>
</html>
{% endraw %}'''


def load_state() -> dict:
    """Load workflow state."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed": [], "runs": []}


def load_flags() -> dict:
    """Load review flags."""
    if FLAGS_FILE.exists():
        try:
            with open(FLAGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_flags(flags: dict):
    """Save review flags."""
    with open(FLAGS_FILE, "w") as f:
        json.dump(flags, f, indent=2)


@app.route('/api/flags')
def get_flags():
    """Get all flags."""
    return jsonify({"flags": load_flags()})


@app.route('/api/flags', methods=['POST'])
def set_flag():
    """Set a flag for a run."""
    data = request.json
    output_dir = data.get("output_dir", "")
    flagged = data.get("flagged", False)

    flags = load_flags()
    if flagged:
        flags[output_dir] = True
    else:
        flags.pop(output_dir, None)

    save_flags(flags)
    return jsonify({"success": True})


# ============ Version Control ============

def get_versions_dir(output_dir: str) -> Path:
    """Get the versions directory for a run."""
    return BASE_DIR / output_dir / "versions"


def create_version_backup(output_dir: str, fix_prompt: str = "", chat_history: list = None) -> str:
    """Create a backup of current state before applying a fix. Returns version ID."""
    import shutil

    full_path = BASE_DIR / output_dir
    versions_dir = get_versions_dir(output_dir)
    versions_dir.mkdir(exist_ok=True)

    # Generate version ID (timestamp-based)
    version_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    version_path = versions_dir / version_id
    version_path.mkdir(exist_ok=True)

    # Copy key files
    files_to_backup = [
        ("src/App.jsx", "App.jsx"),
        ("final.png", "final.png"),
        ("annotated.png", "annotated.png"),
        ("annotated_partial.png", "annotated_partial.png"),
        ("requires.json", "requires.json"),
    ]

    for src_rel, dst_name in files_to_backup:
        src_file = full_path / src_rel
        if src_file.exists():
            shutil.copy(src_file, version_path / dst_name)

    # Save metadata
    metadata = {
        "version_id": version_id,
        "timestamp": datetime.now().isoformat(),
        "fix_prompt": fix_prompt,
        "chat_history": chat_history or []
    }
    with open(version_path / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"[VERSION] Created backup: {version_id}")
    return version_id


def get_version_list(output_dir: str) -> list:
    """Get list of versions for a run."""
    versions_dir = get_versions_dir(output_dir)
    if not versions_dir.exists():
        return []

    versions = []
    for v_dir in sorted(versions_dir.iterdir(), reverse=True):
        if v_dir.is_dir():
            metadata_file = v_dir / "metadata.json"
            if metadata_file.exists():
                with open(metadata_file) as f:
                    metadata = json.load(f)
                versions.append(metadata)
            else:
                versions.append({
                    "version_id": v_dir.name,
                    "timestamp": v_dir.name,
                    "fix_prompt": ""
                })
    return versions


def restore_version(output_dir: str, version_id: str) -> dict:
    """Restore a previous version."""
    import shutil

    full_path = BASE_DIR / output_dir
    version_path = get_versions_dir(output_dir) / version_id

    if not version_path.exists():
        return {"success": False, "error": "Version not found"}

    # Restore files
    files_to_restore = [
        ("App.jsx", "src/App.jsx"),
        ("final.png", "final.png"),
        ("annotated.png", "annotated.png"),
        ("annotated_partial.png", "annotated_partial.png"),
        ("requires.json", "requires.json"),
    ]

    for src_name, dst_rel in files_to_restore:
        src_file = version_path / src_name
        dst_file = full_path / dst_rel
        if src_file.exists():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src_file, dst_file)

    print(f"[VERSION] Restored: {version_id}")
    return {"success": True}


@app.route('/api/versions')
def get_versions():
    """Get versions for a run."""
    output_dir = request.args.get('output_dir', '')
    if not output_dir:
        return jsonify({"versions": []})

    versions = get_version_list(output_dir)
    return jsonify({"versions": versions})


@app.route('/api/restore-version', methods=['POST'])
def restore_version_endpoint():
    """Restore a previous version."""
    data = request.json
    output_dir = data.get("output_dir", "")
    version_id = data.get("version_id", "")

    result = restore_version(output_dir, version_id)
    return jsonify(result)


# ============ Chat History ============

def get_chat_history_path(output_dir: str) -> Path:
    """Get path to chat history file for a run."""
    return BASE_DIR / output_dir / "chat_history.json"


def load_chat_history(output_dir: str) -> list:
    """Load chat history for a run."""
    path = get_chat_history_path(output_dir)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_chat_history(output_dir: str, history: list):
    """Save chat history for a run."""
    path = get_chat_history_path(output_dir)
    with open(path, "w") as f:
        json.dump(history, f, indent=2)


@app.route('/api/chat-history')
def get_chat_history_endpoint():
    """Get chat history for a run."""
    output_dir = request.args.get('output_dir', '')
    if not output_dir:
        return jsonify({"history": []})

    history = load_chat_history(output_dir)
    return jsonify({"history": history})


@app.route('/api/chat-history', methods=['POST'])
def save_chat_history_endpoint():
    """Save chat history for a run."""
    data = request.json
    output_dir = data.get("output_dir", "")
    history = data.get("history", [])

    print(f"[CHAT] Saving {len(history)} messages to {output_dir}")
    save_chat_history(output_dir, history)
    return jsonify({"success": True})


# ============ Queue Persistence ============

def load_queue_state() -> dict:
    """Load queue state from file."""
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"queue": [], "lastCompleted": None}


def save_queue_state(queue: list, last_completed: dict = None):
    """Save queue state to file."""
    with open(QUEUE_FILE, "w") as f:
        json.dump({"queue": queue, "lastCompleted": last_completed}, f, indent=2)


@app.route('/api/queue')
def get_queue():
    """Get persisted queue state."""
    state = load_queue_state()
    return jsonify(state)


@app.route('/api/queue', methods=['POST'])
def save_queue():
    """Save queue state."""
    data = request.json
    queue = data.get("queue", [])
    last_completed = data.get("lastCompleted", None)
    save_queue_state(queue, last_completed)
    return jsonify({"success": True})


@app.route('/api/version-count')
def get_version_counts():
    """Get version counts for all runs."""
    state = load_state()
    counts = {}

    for run in state.get("runs", []):
        output_dir = run.get("output_dir", "")
        versions_dir = get_versions_dir(output_dir)
        if versions_dir.exists():
            counts[output_dir] = len([d for d in versions_dir.iterdir() if d.is_dir()])
        else:
            counts[output_dir] = 0

    return jsonify({"counts": counts})


# ============ Reproduction Costs ============

def get_reproduction_cost(output_dir: str) -> dict:
    """Get reproduction cost from reproduction.log."""
    log_path = BASE_DIR / output_dir / "reproduction.log"
    if not log_path.exists():
        return {"cost": 0, "duration": 0}

    try:
        with open(log_path) as f:
            data = json.load(f)

        total_cost = data.get("cost", {}).get("total_cost", 0)
        duration = data.get("total_duration_sec", 0)
        return {"cost": total_cost, "duration": duration}
    except Exception:
        return {"cost": 0, "duration": 0}


@app.route('/api/reproduction-costs')
def get_all_reproduction_costs():
    """Get reproduction costs for all runs."""
    state = load_state()
    costs = {}
    total_cost = 0
    total_duration = 0

    for run in state.get("runs", []):
        output_dir = run.get("output_dir", "")
        cost_info = get_reproduction_cost(output_dir)
        costs[output_dir] = cost_info
        total_cost += cost_info["cost"]
        total_duration += cost_info["duration"]

    return jsonify({
        "costs": costs,
        "total_cost": total_cost,
        "total_duration": total_duration
    })


def get_unprocessed_images() -> list[Path]:
    """Get list of desktop images that haven't been processed."""
    images_dir = BASE_DIR / "data" / "ui_images"
    if not images_dir.exists():
        return []

    state = load_state()
    processed = set(state.get("processed", []))

    desktop_images = []
    for page_type_dir in images_dir.iterdir():
        if not page_type_dir.is_dir():
            continue
        for img_file in page_type_dir.glob("*-desktop.png"):
            rel_path = str(img_file.relative_to(BASE_DIR))
            if rel_path not in processed:
                desktop_images.append(img_file)

    return sorted(desktop_images)


def parse_output_dir(output_dir: str) -> dict:
    """Parse output directory path into components."""
    # ui_reproducer/output/desktop/{company}/{page_type}/{image_id}/{timestamp}
    parts = Path(output_dir).parts
    try:
        idx = parts.index("output")
        return {
            "device": parts[idx + 1] if len(parts) > idx + 1 else "unknown",
            "company": parts[idx + 2] if len(parts) > idx + 2 else "unknown",
            "page_type": parts[idx + 3] if len(parts) > idx + 3 else "unknown",
            "image_id": parts[idx + 4] if len(parts) > idx + 4 else "unknown",
            "timestamp": parts[idx + 5] if len(parts) > idx + 5 else "unknown",
        }
    except (ValueError, IndexError):
        return {"company": "unknown", "page_type": "unknown", "image_id": "unknown", "timestamp": "unknown"}


@app.route('/')
def index():
    html = HTML_TEMPLATE.replace("__BASE_DIR_JSON__", json.dumps(BASE_DIR_FOR_JS))
    return render_template_string(html)


@app.route('/api/runs')
def get_runs():
    """Get list of all runs with metadata - only latest per image_id."""
    state = load_state()

    # Group by image_id, keep only the latest (highest timestamp)
    latest_by_id = {}
    for run in state.get("runs", []):
        output_dir = run.get("output_dir", "")
        parsed = parse_output_dir(output_dir)
        image_id = parsed["image_id"]
        timestamp = run.get("timestamp", "")

        # Keep if this is newer than what we have
        if image_id not in latest_by_id or timestamp > latest_by_id[image_id]["timestamp"]:
            latest_by_id[image_id] = {
                "image": run.get("image", ""),
                "output_dir": output_dir,
                "timestamp": timestamp,
                "company": parsed["company"].replace("-", " ").title(),
                "page_type": parsed["page_type"].replace("-", " ").title(),
                "image_id": image_id,
            }

    # Sort by timestamp descending (most recent first)
    runs = sorted(latest_by_id.values(), key=lambda x: x["timestamp"], reverse=True)

    return jsonify({"runs": runs})


@app.route('/api/image')
def get_image():
    """Serve image file."""
    output_dir = request.args.get('output_dir', '')
    img_type = request.args.get('type', 'original')

    if not output_dir:
        return "Missing output_dir", 400

    full_path = BASE_DIR / output_dir

    type_map = {
        'original': 'original.png',
        'final': 'final.png',
        'annotated': 'annotated.png',
        'annotated_partial': 'annotated_partial.png',
    }

    img_file = full_path / type_map.get(img_type, 'original.png')

    if img_file.exists():
        return send_file(img_file, mimetype='image/png')
    else:
        return f"Image not found: {img_file}", 404


@app.route('/api/code')
def get_code():
    """Get App.jsx code for a run."""
    output_dir = request.args.get('output_dir', '')

    if not output_dir:
        return jsonify({"code": "// Missing output_dir"})

    full_path = BASE_DIR / output_dir / "src" / "App.jsx"

    if full_path.exists():
        with open(full_path) as f:
            return jsonify({"code": f.read()})
    else:
        return jsonify({"code": f"// File not found: {full_path}"})


@app.route('/api/batch/start', methods=['POST'])
def start_batch():
    """Start a batch of test workflow runs."""
    global batch_status

    if batch_status["running"]:
        return jsonify({"error": "Batch already running"}), 400

    data = request.json
    count = min(data.get("count", 10), 50)  # Cap at 50

    unprocessed = get_unprocessed_images()
    if not unprocessed:
        return jsonify({"error": "No unprocessed images available"}), 400

    images_to_process = unprocessed[:count]

    batch_status = {
        "running": True,
        "total": len(images_to_process),
        "completed": 0,
        "current_image": None,
        "results": [],
        "errors": []
    }

    def run_batch_thread():
        global batch_status

        for img_path in images_to_process:
            if not batch_status["running"]:
                break

            batch_status["current_image"] = str(img_path.relative_to(BASE_DIR))

            try:
                rel_path = str(img_path.relative_to(BASE_DIR))
                cmd = [
                    sys.executable, "test_workflow.py",
                    "--image", rel_path,
                    "--iterations", "2"
                ]

                result = subprocess.run(
                    cmd,
                    cwd=str(SCRIPT_DIR),
                    capture_output=True,
                    text=True,
                    timeout=600  # 10 minute timeout
                )

                if result.returncode == 0:
                    batch_status["results"].append({"image": rel_path, "success": True})
                else:
                    batch_status["errors"].append({"image": rel_path, "error": result.stderr[-500:]})

            except subprocess.TimeoutExpired:
                batch_status["errors"].append({"image": str(img_path), "error": "Timeout"})
            except Exception as e:
                batch_status["errors"].append({"image": str(img_path), "error": str(e)})

            batch_status["completed"] += 1

        batch_status["running"] = False
        batch_status["current_image"] = None

    thread = threading.Thread(target=run_batch_thread, daemon=True)
    thread.start()

    return jsonify({"started": True, "count": len(images_to_process)})


@app.route('/api/batch/status')
def batch_status_endpoint():
    """Get batch processing status."""
    return jsonify(batch_status)


@app.route('/api/chat', methods=['POST'])
def chat():
    """Chat with Claude about the code."""
    data = request.json
    message = data.get("message", "")
    output_dir = data.get("output_dir", "")
    history = data.get("history", [])

    # Load the code
    code_path = BASE_DIR / output_dir / "src" / "App.jsx"
    data_path = BASE_DIR / output_dir / "src" / "data.json"

    code = ""
    if code_path.exists():
        with open(code_path) as f:
            code = f.read()

    data_json = ""
    if data_path.exists():
        with open(data_path) as f:
            data_json = f.read()

    # Build prompt for Claude
    history_text = ""
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        history_text += f"\n{role}: {msg['content']}\n"

    prompt = f"""You are helping review a UI reproduction. The user is looking at a React component that was generated to reproduce a screenshot.

Here is the App.jsx code:
```jsx
{code}
```

Here is the data.json being used:
```json
{data_json[:2000]}...
```

{history_text}
User: {message}

Answer questions about why the code looks the way it does, potential issues, or how to fix problems. Be concise."""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json", prompt],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=120
        )

        response_text = ""
        cost_usd = 0
        try:
            output = json.loads(result.stdout)
            response_text = output.get("result", "")
            cost_usd = output.get("total_cost_usd", 0)
        except json.JSONDecodeError:
            response_text = result.stdout.strip() or result.stderr.strip() or "No response from Claude"

        return jsonify({"response": response_text, "cost_usd": cost_usd})
    except Exception as e:
        return jsonify({"response": f"Error calling Claude: {str(e)}", "cost_usd": 0})


def read_assets_config() -> str:
    """Read brands.json files for company logos and payment methods."""
    assets_dir = BASE_DIR / "data" / "assets_lite"
    assets_info = []

    # Company logos
    logos_brands = assets_dir / "company_logos" / "brands.json"
    if logos_brands.exists():
        try:
            with open(logos_brands) as f:
                data = json.load(f)
                assets_info.append("Company Logos (public/company_logos/):")
                for logo in data.get("logos", [])[:15]:  # Limit to first 15
                    note = f" - {logo['notes']}" if logo.get('notes') else ""
                    assets_info.append(f"  {logo['company']}: {logo['filename']}{note}")
        except Exception:
            pass

    # Payment methods
    payment_brands = assets_dir / "payment_methods" / "brands.json"
    if payment_brands.exists():
        try:
            with open(payment_brands) as f:
                data = json.load(f)
                assets_info.append("Payment Methods (public/payment_methods/):")
                for logo in data.get("logos", []):
                    note = f" - {logo['notes']}" if logo.get('notes') else ""
                    assets_info.append(f"  {logo['company']}: {logo['filename']}{note}")
        except Exception:
            pass

    return "\n".join(assets_info) if assets_info else ""


def build_fix_prompt(user_request: str, output_dir: str) -> str:
    """Build a comprehensive fix prompt with context from the reproduction system."""
    assets_config = read_assets_config()

    return f"""You are fixing a UI reproduction React component.

**YOUR TASK:** {user_request}

IMPORTANT: ONLY do what is requested above. Nothing more. The context below is reference information ONLY - do not proactively "fix" or "improve" things not mentioned in the task.

---
**REFERENCE CONTEXT** (use only if relevant to the task):

DATA SUBSTITUTION RULES:
- All PII uses `data.PII_*`:
  - Name: FULLNAME, FIRSTNAME, LASTNAME
  - Credentials: LOGIN_USERNAME, LOGIN_PASSWORD, LOGIN_PASSWORD_CONFIRM
  - DOB: DOB (MM/DD/YYYY), DOB_ISO, DOB_LONG, DOB_MONTH, DOB_DAY, DOB_YEAR
  - Business: COMPANY, PO_NUMBER, JOB_CODE (50% optional)
  - Contact: EMAIL, PHONE, PHONE_AREA/PREFIX/LINE/SUFFIX
  - Alt phone: PHONE_ALT, PHONE_ALT_AREA/PREFIX/LINE/SUFFIX (50% optional)
  - Address: STREET, STREET_2 (optional), CITY, STATE, STATE_ABBR, POSTCODE, POSTCODE_EXT, POSTCODE_FULL, COUNTRY, COUNTRY_CODE
  - Composites: ADDRESS, CITY_STATE, CITY_STATE_ZIP, CITY_STATE_ZIP_2
  - Card: CARD_TYPE, CARD_IMAGE (logo), CARD_NUMBER, CARD_LAST4, CARD_EXPIRY, CARD_EXPIRY_MONTH/YEAR, CARD_CVV
  - **Card logos**: Use `<img src={{{{data.PII_CARD_IMAGE}}}} />` - never recreate with styled divs
  - Delivery: SECURITY_CODE, DELIVERY_INSTRUCTIONS
  - Gift: GIFT_FIRSTNAME, GIFT_LASTNAME, GIFT_FULLNAME, GIFT_EMAIL, GIFT_MESSAGE
  - Promo: PROMO_CODE
  - Tracking locations: CITY2, CITY3, STATE2, STATE3, CITY_STATE2, CITY_STATE3
  - Nearby 25 locations (stores/lockers): LOCATION{{N}}_STREET, _CITY, _POSTCODE, _POSTCODE_EXT, _POSTCODE_FULL, _CITY_STATE_ZIP
  - AVATAR for profile images
- Products use `data.PRODUCT{{N}}_*`: NAME, PRICE, IMAGE, BRAND, QUANTITY, DESC, RATING, NUM_RATINGS, ITEM_CATEGORY, BREADCRUMB (array)
- PRICE is a **number** (float), format with `.toFixed(2)`
- **Product images**: Use `<img src={{{{data.PRODUCT{{N}}_IMAGE}}}} />` - never recreate with styled divs
- Derived product values (calculate, not in data): PRODUCT1_ORIGINAL_PRICE (e.g., price * 1.25)
- Order dates: ORDER_DATE, ORDER_SHIPPING_DATE, ORDER_DELIVERY_DATE, ORDER_RETURN_BY_DATE
- Calculated order values: ORDER_TOTAL, ORDER_SUBTOTAL, ORDER_TAX, ORDER_SHIPPING_COST, ORDER_NUM_ITEMS
- Cart values: CART_NUM_ITEMS, CART_SUBTOTAL (mark with data-order)
- Generated IDs: `import {{ createGenerators }} from '@generators'; const gen = createGenerators(data.SEED); gen.id('###-####')`

**CRITICAL**: DATA ATTRIBUTES (for bounding box detection):
- `data-pii="PII_*"` on PII values
- `data-product="PRODUCT*_*"` on product values
- `data-order="ORDER_*"` on order values
- `data-search="HEADER_SEARCH"` on search inputs
- Attributes wrap ONLY the value, not labels: `Email: <span data-pii="PII_EMAIL">{{data.PII_EMAIL}}</span>`

INPUT FIELDS:
```jsx
import {{ getPartialProps, getSelectProps }} from './partialFill'
<input data-pii="PII_EMAIL" {{...getPartialProps('PII_EMAIL')}} />
<select data-pii="PII_STATE" {{...getSelectProps('PII_STATE')}}>
  <option value="">Select State</option>
  <option value={{data.PII_STATE}}>{{data.PII_STATE}}</option>
</select>
```

LAYOUT RULES:
- Use Tailwind CSS with arbitrary values (`w-[340px]`, `text-[13px]`)
- Modals/sidebars: full page height (not viewport), use `pointer-events-none` on backdrop, `pointer-events-auto` on content
- NEVER use `fixed bottom-0` for sticky buttons - breaks full-page screenshots

ICONS: Use `lucide-react`, `@heroicons/react`, or `phosphor-react` (no emojis)

AVAILABLE ASSETS:
{assets_config}

---
**IMPORTANT:** Only edit `{output_dir}/src/App.jsx`. Do NOT read or explore other files. If you need additional information, ask instead of searching.

Apply ONLY the requested fix. Briefly explain what you changed."""


@app.route('/api/fix', methods=['POST'])
def apply_fix():
    """Apply a fix to the code using claude CLI."""
    data = request.json
    prompt = data.get("prompt", "")
    output_dir = data.get("output_dir", "")
    chat_history = data.get("chat_history", [])

    full_path = BASE_DIR / output_dir
    code_path = full_path / "src" / "App.jsx"

    if not code_path.exists():
        return jsonify({"success": False, "error": "Code file not found"})

    # Create version backup BEFORE applying fix
    print(f"\n[FIX] Creating version backup...")
    version_id = create_version_backup(output_dir, prompt, chat_history)

    # Build the fix prompt with context
    fix_prompt = build_fix_prompt(prompt, output_dir)

    # Use claude CLI to apply the fix
    try:
        print(f"\n[FIX] ========== Applying fix via claude ==========")
        print(f"[FIX] Prompt: {prompt[:100]}...")

        result = subprocess.run(
            [
                "claude", "-p",
                "--allowedTools", "Edit", "Read",
                "--permission-mode", "acceptEdits",
                "--output-format", "json",
                fix_prompt
            ],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        # Parse JSON output for cost and result
        model_output = ""
        cost_usd = 0
        try:
            output = json.loads(result.stdout)
            model_output = output.get("result", "")
            cost_usd = output.get("total_cost_usd", 0)
        except json.JSONDecodeError:
            model_output = result.stdout + result.stderr

        print(f"[FIX] claude output:\n{model_output[:500]}...")
        print(f"[FIX] Cost: ${cost_usd:.4f}")

        if result.returncode != 0:
            return jsonify({
                "success": False,
                "error": f"claude failed: {result.stderr[-500:]}",
                "model_output": model_output,
                "cost_usd": cost_usd
            })

        # claude directly edits the file, so just regenerate requires.json
        print(f"[FIX] claude completed, regenerating requires.json...")
        sys.path.insert(0, str(SCRIPT_DIR))
        from reproduce_ui import generate_requires_json
        generate_requires_json(full_path)

        # Copy fresh data.json from template
        import shutil
        template_data_path = SCRIPT_DIR / "template" / "src" / "data.json"
        output_data_path = full_path / "src" / "data.json"
        if template_data_path.exists():
            shutil.copy(template_data_path, output_data_path)

        # Rerender - take new screenshots
        print(f"[FIX] Starting rerender...")
        rerender_result = rerender_output(output_dir)

        return jsonify({
            "success": rerender_result["success"],
            "error": rerender_result.get("error", ""),
            "model_output": model_output,
            "version_id": version_id,
            "cost_usd": cost_usd
        })

    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": f"{str(e)}\n{traceback.format_exc()[-500:]}"})


@app.route('/api/rerender', methods=['POST'])
def rerender():
    """Rerender screenshots without changing code."""
    data = request.json
    output_dir = data.get("output_dir", "")

    result = rerender_output(output_dir)
    return jsonify(result)


@app.route('/api/delete-run', methods=['POST'])
def delete_run():
    """Delete a run directory and remove from state."""
    data = request.json
    output_dir = data.get("output_dir", "")

    full_path = BASE_DIR / output_dir

    if not full_path.exists():
        return jsonify({"success": False, "error": "Directory not found"})

    try:
        import shutil
        print(f"[DELETE] Deleting: {output_dir}")
        shutil.rmtree(full_path)

        # Remove from state file
        state = load_state()
        state["runs"] = [r for r in state.get("runs", []) if r.get("output_dir") != output_dir]
        # Also remove from processed list if present
        image_path = next((r.get("image") for r in state.get("runs", []) if r.get("output_dir") == output_dir), None)
        if image_path and image_path in state.get("processed", []):
            state["processed"].remove(image_path)

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        print(f"[DELETE] Successfully deleted and updated state")
        return jsonify({"success": True})

    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": f"{str(e)}\n{traceback.format_exc()[-500:]}"})


@app.route('/api/refresh-and-rerender', methods=['POST'])
def refresh_and_rerender():
    """Regenerate requires.json, refresh data.json from template, then rerender."""
    data = request.json
    output_dir = data.get("output_dir", "")

    full_path = BASE_DIR / output_dir

    if not full_path.exists():
        return jsonify({"success": False, "error": "Output directory not found"})

    try:
        print(f"\n[REFRESH] ========== Refreshing data for: {output_dir} ==========")

        # Regenerate requires.json from App.jsx
        sys.path.insert(0, str(SCRIPT_DIR))
        from reproduce_ui import generate_requires_json
        print(f"[REFRESH] Regenerating requires.json...")
        generate_requires_json(full_path)

        # Copy fresh data.json from template
        import shutil
        template_data_path = SCRIPT_DIR / "template" / "src" / "data.json"
        output_data_path = full_path / "src" / "data.json"
        if template_data_path.exists():
            shutil.copy(template_data_path, output_data_path)
            print(f"[REFRESH] Copied fresh data.json from template")
        else:
            print(f"[REFRESH] WARNING: Template data.json not found at {template_data_path}")

        # Now rerender
        print(f"[REFRESH] Starting rerender...")
        result = rerender_output(output_dir)

        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": f"{str(e)}\n{traceback.format_exc()[-500:]}"})


def rerender_output(output_dir: str) -> dict:
    """Take new screenshots for an output directory."""
    import socket

    print(f"\n[RERENDER] ========== Starting rerender for: {output_dir} ==========")
    full_path = BASE_DIR / output_dir
    print(f"[RERENDER] Full path: {full_path}")
    print(f"[RERENDER] Path exists: {full_path.exists()}")

    if not full_path.exists():
        print(f"[RERENDER] ERROR: Directory not found!")
        return {"success": False, "error": "Output directory not found"}

    try:
        # Import screenshot function
        sys.path.insert(0, str(SCRIPT_DIR))
        from screenshot_pages import take_screenshot_with_annotations, inject_data_json
        from test_workflow import find_free_port

        # Load data
        data_path = full_path / "src" / "data.json"
        requires_path = full_path / "requires.json"

        with open(data_path) as f:
            data = json.load(f)

        required_fields = []
        if requires_path.exists():
            with open(requires_path) as f:
                requires = json.load(f)
                required_fields = requires.get("required_fields", [])

        # Parse path info
        parsed = parse_output_dir(output_dir)

        page_info = {
            "path": full_path,
            "company": parsed["company"],
            "page_type": parsed["page_type"],
            "device": parsed["device"],
            "required_fields": required_fields,
            "output_dir": output_dir
        }

        # Take full screenshot
        print(f"[RERENDER] Taking annotated screenshot for {output_dir}")
        port = find_free_port()
        annotation = take_screenshot_with_annotations(
            page_info=page_info,
            data=data,
            scroll_y=0,
            output_dir=full_path,
            index=9999,
            port=port,
            full_page=True,
            partial_fill=False
        )
        print(f"[RERENDER] Screenshot taken, checking for 9999.png")

        # Replace files (delete old first to ensure overwrite)
        old_png = full_path / "9999.png"
        old_json = full_path / "9999.json"
        print(f"[RERENDER] 9999.png exists: {old_png.exists()}")
        if old_png.exists():
            target = full_path / "annotated.png"
            if target.exists():
                target.unlink()
            old_png.rename(target)
            print(f"[RERENDER] Renamed to annotated.png")
        if old_json.exists():
            target = full_path / "annotated.json"
            if target.exists():
                target.unlink()
            old_json.rename(target)

        # Take partial screenshot
        print(f"[RERENDER] Taking partial screenshot")
        port = find_free_port()
        take_screenshot_with_annotations(
            page_info=page_info,
            data=data,
            scroll_y=0,
            output_dir=full_path,
            index=9998,
            port=port,
            full_page=True,
            partial_fill=True
        )

        old_partial_png = full_path / "9998.png"
        old_partial_json = full_path / "9998.json"
        print(f"[RERENDER] 9998.png exists: {old_partial_png.exists()}")
        if old_partial_png.exists():
            target = full_path / "annotated_partial.png"
            if target.exists():
                target.unlink()
            old_partial_png.rename(target)
            print(f"[RERENDER] Renamed to annotated_partial.png")
        if old_partial_json.exists():
            target = full_path / "annotated_partial.json"
            if target.exists():
                target.unlink()
            old_partial_json.rename(target)

        # Restore full data BEFORE taking final.png (partial screenshot left partial config)
        inject_data_json(full_path, data, partial_fill=False)

        # Take clean final.png (no bounding boxes)
        print(f"[RERENDER] Taking final screenshot")
        from reproduce_ui import take_screenshot, start_dev_server
        from screenshot_pages import kill_process_tree
        port = find_free_port()
        server_proc = start_dev_server(full_path, port)
        try:
            take_screenshot(full_path, "final", port)
            print(f"[RERENDER] Final screenshot done")
        finally:
            kill_process_tree(server_proc)

        print(f"[RERENDER] SUCCESS for {output_dir}")
        return {"success": True}

    except Exception as e:
        import traceback
        return {"success": False, "error": f"{str(e)}\n{traceback.format_exc()}"}


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("UI Reproduction Batch Review GUI")
    print("=" * 60)
    print(f"Open in browser: http://localhost:5050")
    print("=" * 60 + "\n")

    import webbrowser
    webbrowser.open("http://localhost:5050")

    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
