// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import type { ChecklistStep } from './types';
import styles from './Checklist.module.css';

interface ChecklistProps {
  steps: ChecklistStep[];
  onStepClicked?: (stepId: string) => void;
}

export default function Checklist({ steps, onStepClicked }: ChecklistProps) {
  return (
    <ul className={styles.checklist}>
      {steps.map((step, idx) => {
        const cls = [
          styles['checklist-item'],
          step.status === 'active' ? styles['checklist-item--active'] : '',
          step.status === 'done' ? styles['checklist-item--done'] : '',
        ]
          .filter(Boolean)
          .join(' ');

        return (
          <li
            key={step.id}
            className={cls}
            onClick={() => onStepClicked?.(step.id)}
          >
            <span className={styles['checklist-icon']}>
              {step.status === 'active' && (
                <span className={styles['checklist-spinner']} />
              )}
              {step.status === 'done' && (
                <span className={styles['checklist-check']}>✓</span>
              )}
              {step.status === 'pending' && (
                <span>{idx + 1}</span>
              )}
            </span>
            <span>{step.title}</span>
          </li>
        );
      })}
    </ul>
  );
}
