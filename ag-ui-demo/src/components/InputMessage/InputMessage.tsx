// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import CheckboxGroup from './CheckboxGroup';
import TableInput from './TableInput';

// ── mode payloads ────────────────────────────────────────

export interface YesNoMode {
  mode: 'yesno';
  question: string;
}

export interface CheckboxMode {
  mode: 'checkbox';
  question: string;
  options: { label: string; value: string; checked?: boolean }[];
}

export interface TableMode {
  mode: 'table';
  question: string;
  columns: { key: string; label: string }[];
  rows?: Record<string, string>[];
}

export type InputMode = YesNoMode | CheckboxMode | TableMode;

interface InputMessageProps {
  input: InputMode;
  onSubmit: (result: unknown) => void;
}

export default function InputMessage({ input, onSubmit }: InputMessageProps) {
  return (
    <div className="input-message">
      <p className="input-message__question">{input.question}</p>

      {input.mode === 'yesno' && (
        <div className="input-message__buttons">
          <button className="btn btn--primary" onClick={() => onSubmit({ answer: 'yes' })}>
            Yes
          </button>
          <button className="btn btn--secondary" onClick={() => onSubmit({ answer: 'no' })}>
            No
          </button>
        </div>
      )}

      {input.mode === 'checkbox' && (
        <CheckboxGroup
          options={input.options}
          onSubmit={(selected) => onSubmit({ selected })}
        />
      )}

      {input.mode === 'table' && (
        <TableInput
          columns={input.columns}
          rows={input.rows ?? []}
          onSubmit={(rows) => onSubmit({ rows })}
        />
      )}
    </div>
  );
}
