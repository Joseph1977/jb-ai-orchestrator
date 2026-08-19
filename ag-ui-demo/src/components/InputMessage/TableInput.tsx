// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react';

interface TableColumn {
  key: string;
  label: string;
}

interface TableInputProps {
  columns: TableColumn[];
  rows: Record<string, string>[];
  onSubmit: (rows: Record<string, string>[]) => void;
}

export default function TableInput({ columns, rows: initialRows, onSubmit }: TableInputProps) {
  const [rows, setRows] = useState<Record<string, string>[]>(
    initialRows.length > 0 ? initialRows : [Object.fromEntries(columns.map((c) => [c.key, '']))],
  );

  function update(rowIdx: number, colKey: string, value: string) {
    setRows((prev) =>
      prev.map((r, i) => (i === rowIdx ? { ...r, [colKey]: value } : r)),
    );
  }

  function addRow() {
    setRows((prev) => [...prev, Object.fromEntries(columns.map((c) => [c.key, '']))]);
  }

  return (
    <div className="table-input">
      <table>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {columns.map((col) => (
                <td key={col.key}>
                  <input
                    type="text"
                    value={row[col.key] ?? ''}
                    onChange={(e) => update(ri, col.key, e.target.value)}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="table-input__actions">
        <button className="btn btn--secondary" onClick={addRow}>
          + Row
        </button>
        <button className="btn btn--primary" onClick={() => onSubmit(rows)}>
          Submit
        </button>
      </div>
    </div>
  );
}
