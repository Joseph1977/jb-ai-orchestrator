// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react';

interface CheckboxOption {
  label: string;
  value: string;
  checked?: boolean;
}

interface CheckboxGroupProps {
  options: CheckboxOption[];
  onSubmit: (selected: string[]) => void;
}

export default function CheckboxGroup({ options, onSubmit }: CheckboxGroupProps) {
  const [selected, setSelected] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    options.forEach((o) => {
      if (o.checked) initial.add(o.value);
    });
    return initial;
  });

  function toggle(value: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return next;
    });
  }

  return (
    <div className="checkbox-group">
      {options.map((opt) => (
        <label key={opt.value} className="checkbox-group__item">
          <input
            type="checkbox"
            checked={selected.has(opt.value)}
            onChange={() => toggle(opt.value)}
          />
          <span>{opt.label}</span>
        </label>
      ))}
      <button
        className="btn btn--primary"
        onClick={() => onSubmit(Array.from(selected))}
      >
        Submit
      </button>
    </div>
  );
}
