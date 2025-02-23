let editor;
let socket;
const MAX_CODE_LENGTH = 50000;
let isExecuting = false;
let lastError = null;
let lastCode = null;
let currentProcessRunning = false;

socket = io({
    reconnection: true,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    reconnectionAttempts: 5
});

socket.on('connect', function() {
    console.log('Connected to server');
    updateStatusIndicator(true);
});

socket.on('disconnect', function() {
    console.log('Disconnected from server');
    updateStatusIndicator(false);
});

function updateStatusIndicator(connected) {
    const statusIndicator = document.getElementById('connection-status');
    if (statusIndicator) {
        statusIndicator.className = connected ? 'status-connected' : 'status-disconnected';
        statusIndicator.title = connected ? 'Connected' : 'Disconnected';
    }
}

socket.on('execution_output', function(data) {
    const output = document.getElementById('output');
    const helpButton = document.getElementById('getHelpButton');
    
    if (data.error) {
        output.innerHTML += `<span class="error-text">${escapeHtml(data.error)}</span>\n`;
        if (data.error.includes('Traceback')) {
            lastError = data.error;
            lastCode = editor.getValue();
            helpButton.disabled = false;  
        }
        if (
            data.error === 'Execution cancelled by user.' ||
            data.error === 'No running process found.' ||
            data.error.startsWith("Syntax Error:")
        ) {
            isExecuting = false;
            currentProcessRunning = false;
            updateRunButton();
            updateCancelButton();
        }
    } else if (data.output) {
        output.innerHTML += `<span class="output-text">${escapeHtml(data.output)}</span>`;
    } else if (data.execution_time) {
        output.innerHTML += `<span class="execution-time">${escapeHtml(data.execution_time)}</span>\n`;
        isExecuting = false;
        currentProcessRunning = false;
        updateRunButton();
        updateCancelButton();
    }
    
    output.scrollTop = output.scrollHeight;
});

socket.on('execution_input_request', function(data) {
    const promptText = data.prompt || '';
    const output = document.getElementById('output');

    const promptSpan = document.createElement('span');
    promptSpan.className = 'prompt-text';
    promptSpan.innerText = promptText + " ";
    output.appendChild(promptSpan);

    const inputField = document.createElement('input');
    inputField.type = 'text';
    inputField.className = 'console-input';
    inputField.style.background = 'transparent';
    inputField.style.border = 'none';
    inputField.style.outline = 'none';
    inputField.style.color = 'inherit';
    inputField.style.fontFamily = 'inherit';
    inputField.style.fontSize = 'inherit';
    output.appendChild(inputField);

    inputField.focus();

    inputField.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            let value = inputField.value;
            socket.emit('input_response', { input: value });

            const echoSpan = document.createElement('span');
            echoSpan.className = 'input-echo';
            echoSpan.innerText = value + "\n";
            output.removeChild(inputField);
            output.appendChild(echoSpan);
            output.scrollTop = output.scrollHeight;
        }
    });
});

function updateCancelButton() {
    const cancelButton = document.getElementById('cancelButton');
    if (cancelButton) {
        cancelButton.disabled = !currentProcessRunning;
    }
}

function updateRunButton() {
    const runButton = document.getElementById('run-button');
    if (runButton) {
        runButton.disabled = isExecuting;
        runButton.innerHTML = isExecuting ? 'Running...' : 'Run Code';
    }
}

window.runCode = function() {
    if (!editor || !socket || isExecuting) return;

    const code = editor.getValue();
    
    if (code.length > MAX_CODE_LENGTH) {
        alert('Code exceeds maximum length limit');
        return;
    }

    const forbiddenPatterns = [
        'import os', 'import sys', 'import subprocess',
        'eval(', 'exec(', '__import__', 'open(',
        'system(', 'popen(', 'subprocess'
    ];

    if (forbiddenPatterns.some(pattern => code.includes(pattern))) {
        alert('Forbidden operations detected');
        return;
    }

    isExecuting = true;
    currentProcessRunning = true;
    updateRunButton();
    updateCancelButton();
    
    const output = document.getElementById('output');
    output.innerHTML += '<span class="execution-start">--- Execution Started ---</span>\n';
    
    socket.emit('execute', { code: code });
};

window.clearOutput = function() {
    const output = document.getElementById('output');
    if (output) output.innerHTML = '';
};

window.cancelExecution = function() {
    if (!currentProcessRunning) return;
    socket.emit('cancel_execution');
};

require.config({ 
    paths: { 
        vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.36.1/min/vs' 
    }
});

require(['vs/editor/editor.main'], function() {
    monaco.languages.registerCompletionItemProvider('python', {
        triggerCharacters: ['.', '('],
        provideCompletionItems: function(model, position) {
            return new Promise((resolve) => {
                const textUntilPosition = model.getValueInRange({
                    startLineNumber: 1,
                    startColumn: 1,
                    endLineNumber: position.lineNumber,
                    endColumn: position.column
                });

                const lineContent = model.getLineContent(position.lineNumber);
                const beforePosition = lineContent.substring(0, position.column - 1);

                if (isInsideString(beforePosition)) {
                    resolve({ suggestions: [] });
                    return;
                }

                socket.emit('completion', {
                    text: textUntilPosition,
                    position: {
                        lineNumber: position.lineNumber,
                        column: position.column
                    }
                });

                socket.once('completion_result', function(data) {
                    if (data.suggestions) {
                        const suggestions = data.suggestions
                            .filter(suggestion => {
                                const blacklist = [
                                    'system', 'os', 'path', 'subprocess', 'eval',
                                    'exec', '__import__', 'open', 'file'
                                ];
                                return !blacklist.some(term => 
                                    suggestion.label.toLowerCase().includes(term)
                                );
                            })
                            .map(suggestion => ({
                                label: suggestion.label,
                                kind: monaco.languages.CompletionItemKind[
                                    suggestion.kind || 'Function'
                                ],
                                detail: suggestion.detail,
                                documentation: {
                                    value: suggestion.documentation
                                },
                                insertText: suggestion.insertText,
                                sortText: suggestion.sortText
                            }));
                        resolve({ suggestions });
                    } else {
                        resolve({ suggestions: [] });
                    }
                });
            });
        }
    });

    function isInsideString(text) {
        let singleQuoteCount = 0;
        let doubleQuoteCount = 0;
        let isEscaped = false;

        for (let i = 0; i < text.length; i++) {
            if (text[i] === '\\') {
                isEscaped = !isEscaped;
                continue;
            }
            if (!isEscaped) {
                if (text[i] === "'") singleQuoteCount++;
                if (text[i] === '"') doubleQuoteCount++;
            }
            isEscaped = false;
        }

        return (singleQuoteCount % 2 === 1) || (doubleQuoteCount % 2 === 1);
    }

    editor = monaco.editor.create(document.getElementById('monaco-editor'), {
        value: '# Напишите свой код на Python\n',
        language: 'python',
        theme: 'vs-dark',
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 14,
        lineNumbers: 'on',
        roundedSelection: false,
        scrollBeyondLastLine: false,
        readOnly: false,
        wordWrap: 'off',
        snippetSuggestions: 'inline',
        suggestOnTriggerCharacters: true,
        acceptSuggestionOnEnter: 'smart',
        tabCompletion: 'on',
        wordBasedSuggestions: true,
        parameterHints: { enabled: true },
        suggest: {
            snippetsPreventQuickSuggestions: false,
            showIcons: true,
            showStatusBar: true,
            preview: true,
            maxVisibleSuggestions: 12,
            filterGraceful: true,
            localityBonus: true,
            strings: false 
        },
        hover: { enabled: true },
        quickSuggestions: {
            other: true,
            comments: false,
            strings: false 
        }
    });

    const editorContainer = document.querySelector('.editor-container');
    if (editorContainer) {
        const resizeObserver = new ResizeObserver(() => {
            if (editor) {
                editor.layout();
            }
        });
        resizeObserver.observe(editorContainer);
    }
   
    const resizeHandle = document.querySelector('.resize-handle');
    let isResizing = false;
    let startY = 0;
    let startHeight = 0;

    function initResize(e) {
        isResizing = true;
        startY = e.clientY;
        startHeight = editorContainer.getBoundingClientRect().height;
        document.addEventListener('pointermove', onResize);
        document.addEventListener('pointerup', stopResize);
        e.preventDefault();
    }

    function onResize(e) {
        if (!isResizing) return;
        let clientY = e.clientY;
        let newHeight = startHeight + (clientY - startY);
        const MIN_HEIGHT = 100;
        if (newHeight < MIN_HEIGHT) {
            newHeight = MIN_HEIGHT;
        }
        editorContainer.style.height = newHeight + 'px';
        if (editor) {
            editor.layout();
        }
    }

    function stopResize() {
        isResizing = false;
        document.removeEventListener('pointermove', onResize);
        document.removeEventListener('pointerup', stopResize);
    }

    resizeHandle.addEventListener('pointerdown', initResize);

    let lintingTimeout;
    editor.onDidChangeModelContent(function() {
        clearTimeout(lintingTimeout);
        lintingTimeout = setTimeout(() => {
            const code = editor.getValue();
            socket.emit('lint', { code: code });
        }, 500);
    });
});

function escapeHtml(unsafe) {
    return unsafe
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

document.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        window.runCode();
    }
    
    if ((e.ctrlKey || e.metaKey) && e.key === 'l') {
        e.preventDefault();
        window.clearOutput();
    }
});