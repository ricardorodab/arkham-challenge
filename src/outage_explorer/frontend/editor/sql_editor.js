/**
 * Provide the browser SQL editing surface without granting analytical capabilities.
 *
 * Completion is entirely local and draws identifiers only from the selected
 * authorized catalog entry. Highlighting and completion assist composition;
 * backend parsing, authorization and execution limits remain authoritative.
 * Rebuild the packaged ESM asset with `npm run build:editor` after editing.
 */
import { Compartment, EditorState, Prec } from '@codemirror/state';
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from '@codemirror/view';
import {
  defaultKeymap,
  history,
  historyKeymap,
  isolateHistory,
  indentMore,
  indentLess,
} from '@codemirror/commands';
import {
  bracketMatching,
  HighlightStyle,
  syntaxHighlighting,
  syntaxTree,
} from '@codemirror/language';
import { sql } from '@codemirror/lang-sql';
import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  closeCompletion,
} from '@codemirror/autocomplete';
import { tags } from '@lezer/highlight';

const KEYWORDS = [
  'SELECT',
  'FROM',
  'WHERE',
  'GROUP BY',
  'HAVING',
  'ORDER BY',
  'LIMIT',
  'AS',
  'AND',
  'OR',
  'NOT',
  'IS NULL',
  'IS NOT NULL',
  'BETWEEN',
  'IN',
  'LIKE',
  'ILIKE',
  'ASC',
  'DESC',
  'NULLS FIRST',
  'NULLS LAST',
  'TRUE',
  'FALSE',
  'NULL',
];
const FUNCTIONS = [
  ['COUNT', 'COUNT(*)', 'Count matching rows'],
  ['SUM', 'SUM()', 'Sum non-null values'],
  ['AVG', 'AVG()', 'Average non-null values'],
  ['MIN', 'MIN()', 'Smallest non-null value'],
  ['MAX', 'MAX()', 'Largest non-null value'],
  ['ABS', 'ABS()', 'Absolute numeric value'],
  ['ROUND', 'ROUND()', 'Round a numeric value'],
  ['COALESCE', 'COALESCE()', 'First non-null argument'],
  ['CAST', 'CAST()', 'Convert a value using AS and a supported type'],
];

/** Quote one logical identifier, preserving exact catalog spelling. */
const quote = (name) => `"${String(name).replaceAll('"', '""')}"`;

/**
 * Copy a small selected-schema completion list without retaining mutable catalog state.
 * Unknown, malformed or unavailable entries contribute no identifier suggestions.
 */
export function schemaCompletions(dataset) {
  if (!dataset || typeof dataset.id !== 'string' || !Array.isArray(dataset.schema)) return [];
  const fields = dataset.schema.filter(
    (item) => Array.isArray(item) && typeof item[0] === 'string' && typeof item[1] === 'string',
  );
  return [
    {
      label: dataset.id,
      type: 'class',
      detail: 'Selected dataset',
      apply: quote(dataset.id),
      boost: 2,
    },
    ...fields.map(([name, type]) => ({
      label: name,
      type: 'property',
      detail: type,
      apply: quote(name),
      boost: 3,
    })),
  ];
}

/**
 * Complete supported vocabulary and selected-schema names, avoiding string/comment text.
 * Qualified references keep their existing qualifier. Following FROM, only the
 * selected dataset is suggested. This is writing assistance, not SQL validation.
 */
export function sqlCompletionSource(getDataset) {
  return (context) => {
    const node = syntaxTree(context.state).resolveInner(context.pos, -1);
    if (/String|Comment/.test(node.name)) return null;
    const word = context.matchBefore(/"?[A-Za-z_][A-Za-z0-9_]*|"/);
    if (!word && !context.explicit) return null;
    const from = word?.from ?? context.pos;
    const before = context.state.sliceDoc(0, from);
    const identifiers = schemaCompletions(getDataset());
    const quoted = word?.text.startsWith('"');
    const onlyTable = /\bFROM\s*$/i.test(before);
    const onlyColumns = /\.\s*$/.test(before);
    let options = onlyTable
      ? identifiers.slice(0, 1)
      : onlyColumns || quoted
        ? identifiers.slice(1)
        : [
            ...identifiers,
            ...KEYWORDS.map((label) => ({ label, type: 'keyword' })),
            ...FUNCTIONS.map(([label, insertion, detail]) => ({
              label,
              type: 'function',
              detail,
              apply(view, completion, start, end) {
                const cursor = start + insertion.length - (insertion.endsWith('()') ? 1 : 0);
                view.dispatch({
                  changes: { from: start, to: end, insert: insertion },
                  selection: { anchor: cursor },
                  userEvent: 'input.complete',
                });
              },
            })),
          ];
    if (quoted && onlyTable) options = identifiers.slice(0, 1);
    const suffix = context.state.sliceDoc(context.pos).match(/^[A-Za-z0-9_]*/)[0];
    let to = context.pos + suffix.length;
    if (quoted && context.state.sliceDoc(to, to + 1) === '"') to++;
    if (quoted) {
      const prefix = word.text.slice(1).toLowerCase();
      options = options.filter((option) => option.label.toLowerCase().startsWith(prefix));
      // The quote belongs to the replacement range, not the searchable label.
      return { from, to, options, filter: false };
    }
    return { from, to, options, validFor: /^[A-Za-z_][A-Za-z0-9_]*$/ };
  };
}

// Colors meet normal-text AA contrast against the editor's white background.
const highlighting = HighlightStyle.define([
  { tag: tags.keyword, color: '#643a9b', fontWeight: '600' },
  { tag: [tags.string, tags.special(tags.string)], color: '#16633d' },
  { tag: [tags.number, tags.bool, tags.null], color: '#914819' },
  { tag: tags.comment, color: '#5d6874', fontStyle: 'italic' },
  { tag: [tags.operator, tags.punctuation], color: '#41546a' },
  { tag: [tags.name, tags.propertyName, tags.variableName], color: '#185b85' },
  { tag: tags.typeName, color: '#7b3f16' },
]);

const theme = EditorView.theme({
  '&': {
    backgroundColor: '#ffffff',
    color: '#233341',
    fontSize: '14px',
    border: '1px solid #98a69c',
    borderRadius: '6px',
    overflow: 'hidden',
  },
  '&.cm-focused': { outline: '3px solid #397654', outlineOffset: '2px' },
  '.cm-scroller': {
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace",
    lineHeight: '1.7',
    overflow: 'auto',
    maxHeight: '520px',
  },
  '.cm-scroller:focus-visible': { outline: '2px solid #397654', outlineOffset: '-2px' },
  '.cm-content': { minHeight: '252px', padding: '12px 0', caretColor: '#173b2e' },
  '.cm-line': { padding: '0 14px' },
  '.cm-gutters': { backgroundColor: '#f4f6f5', color: '#65736b', borderRight: '1px solid #e0e5e1' },
  '.cm-lineNumbers .cm-gutterElement': { minWidth: '34px', padding: '0 8px' },
  '.cm-activeLine': { backgroundColor: '#f3f7f4' },
  '.cm-activeLineGutter': { backgroundColor: '#e8efe9', color: '#234a35' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: '#d7e7f5 !important',
  },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#173b2e' },
  '.cm-matchingBracket': { backgroundColor: '#e0eddd', outline: '1px solid #7c9a76' },
  '.cm-tooltip': {
    border: '1px solid #a9b8af',
    borderRadius: '6px',
    backgroundColor: '#fff',
    color: '#233341',
    boxShadow: '0 5px 18px #142d2422',
    maxWidth: 'min(560px, 92vw)',
  },
  '.cm-tooltip-autocomplete > ul': { fontFamily: 'inherit', maxHeight: '230px' },
  '.cm-tooltip-autocomplete > ul > li': { padding: '5px 8px' },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
    backgroundColor: '#1f5c46',
    color: '#fff',
  },
  '.cm-completionDetail': { marginLeft: '14px', fontSize: '12px', opacity: '1' },
  '.cm-completionMatchedText': { textDecoration: 'underline', textUnderlineOffset: '2px' },
  '@media (max-width: 600px)': {
    '.cm-content': { minHeight: '220px' },
    '.cm-line': { padding: '0 8px' },
  },
});

/**
 * Enhance a backing textarea using a self-hosted, nonce-styled CodeMirror view.
 *
 * The textarea remains the canonical form value for existing controller paths.
 * Construction does not take focus. Programmatic value changes should use the
 * returned setValue method; a dispatched textarea input event is also supported.
 * Only a server-issued nonce matching the response's style CSP is accepted.
 */
export function createSqlEditor({ textarea, onChange = () => {}, onRun = () => {}, styleNonce }) {
  if (!textarea || textarea.tagName !== 'TEXTAREA' || !textarea.parentElement) {
    throw new TypeError('SQL editor requires a mounted textarea.');
  }
  if (typeof styleNonce !== 'string' || !/^[A-Za-z0-9+/_=-]{16,}$/.test(styleNonce)) {
    throw new TypeError('SQL editor requires a server-issued style nonce.');
  }
  let dataset = null;
  let destroyed = false;
  const completion = new Compartment();
  const mount = textarea.ownerDocument.createElement('div');
  mount.className = 'sql-code-editor';
  mount.style.minWidth = '0';
  textarea.after(mount);
  const source = sqlCompletionSource(() => dataset);
  const completionConfig = () =>
    autocompletion({
      override: [source],
      activateOnTyping: true,
      interactionDelay: 0,
      maxRenderedOptions: 40,
      closeOnBlur: true,
    });
  const attributes = {
    role: 'textbox',
    'aria-label': 'SQL query',
    'aria-multiline': 'true',
    spellcheck: 'false',
    autocapitalize: 'off',
    autocorrect: 'off',
    id: `${textarea.id}-input`,
  };
  if (textarea.getAttribute('aria-describedby'))
    attributes['aria-describedby'] = textarea.getAttribute('aria-describedby');
  const extensions = () => [
    EditorView.cspNonce.of(styleNonce),
    EditorView.contentAttributes.of(attributes),
    lineNumbers(),
    highlightActiveLineGutter(),
    highlightActiveLine(),
    drawSelection(),
    history(),
    sql(),
    syntaxHighlighting(highlighting),
    bracketMatching(),
    closeBrackets(),
    completion.of(completionConfig()),
    theme,
    Prec.highest(
      keymap.of([
        {
          key: 'Mod-Enter',
          run() {
            onRun();
            return true;
          },
        },
        { key: 'Mod-]', run: indentMore },
        { key: 'Mod-[', run: indentLess },
      ]),
    ),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap]),
    EditorView.updateListener.of((update) => {
      if (!update.docChanged || destroyed) return;
      textarea.value = update.state.doc.toString();
      onChange(textarea.value);
    }),
  ];
  let view;
  try {
    view = new EditorView({ doc: textarea.value, extensions: extensions(), parent: mount });
  } catch (error) {
    mount.remove();
    throw error;
  }
  // Long SQL lines can scroll independently on narrow screens. Give keyboard
  // users a named scrolling stop as well as the separate editable textbox.
  view.scrollDOM.tabIndex = 0;
  view.scrollDOM.setAttribute('role', 'region');
  view.scrollDOM.setAttribute('aria-label', 'SQL editor scroll area');
  textarea.hidden = true;
  const label = textarea.ownerDocument.querySelector(`label[for="${textarea.id}"]`);
  const focusLabel = (event) => {
    event.preventDefault();
    view.focus();
  };
  label?.addEventListener('click', focusLabel);
  const syncInput = () => {
    if (!destroyed && textarea.value !== view.state.doc.toString()) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: textarea.value } });
    }
  };
  textarea.addEventListener('input', syncInput);
  return {
    getValue: () => textarea.value,
    /** Replace a draft; resetting history prevents undo from revealing another dataset's draft. */
    setValue(text, { resetHistory = false } = {}) {
      if (destroyed) return;
      const next = String(text ?? '');
      closeCompletion(view);
      if (resetHistory) {
        view.setState(EditorState.create({ doc: next, extensions: extensions() }));
        textarea.value = next;
        onChange(next);
      } else if (next !== view.state.doc.toString()) {
        view.dispatch({
          changes: { from: 0, to: view.state.doc.length, insert: next },
          selection: { anchor: next.length },
          userEvent: 'input.replace',
          annotations: isolateHistory.of('full'),
        });
      }
    },
    /** Insert a quoted logical name at the retained selection, as one undoable edit. */
    insertIdentifier(name) {
      if (destroyed) return;
      closeCompletion(view);
      const insertion = quote(name);
      view.dispatch({
        ...view.state.replaceSelection(insertion),
        scrollIntoView: true,
        userEvent: 'input.schema',
        annotations: isolateHistory.of('full'),
      });
      view.focus();
    },
    focus() {
      if (!destroyed) view.focus();
    },
    /** Retire all previous completion results when the selected authorized schema changes. */
    setDataset(value) {
      if (destroyed) return;
      dataset = value ? { id: value.id, schema: value.schema?.map((item) => [...item]) } : null;
      closeCompletion(view);
      view.dispatch({ effects: completion.reconfigure(completionConfig()) });
    },
    clear() {
      this.setDataset(null);
      this.setValue('', { resetHistory: true });
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      dataset = null;
      textarea.removeEventListener('input', syncInput);
      label?.removeEventListener('click', focusLabel);
      view.destroy();
      mount.remove();
      textarea.hidden = false;
    },
  };
}
