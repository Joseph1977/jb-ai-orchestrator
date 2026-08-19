// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import Checklist from './Checklist';
import type { ChecklistStep } from './types';

describe('Checklist', () => {
  const steps: ChecklistStep[] = [
    { id: '1', title: 'Analyze', status: 'done' },
    { id: '2', title: 'Process', status: 'active' },
    { id: '3', title: 'Complete', status: 'pending' },
  ];

  it('renders all steps', () => {
    render(<Checklist steps={steps} />);
    expect(screen.getByText('Analyze')).toBeInTheDocument();
    expect(screen.getByText('Process')).toBeInTheDocument();
    expect(screen.getByText('Complete')).toBeInTheDocument();
  });

  it('shows checkmark for done steps', () => {
    render(<Checklist steps={steps} />);
    expect(screen.getByText('✓')).toBeInTheDocument();
  });

  it('shows spinner for active step', () => {
    const { container } = render(<Checklist steps={steps} />);
    expect(container.querySelector('.checklist-spinner')).toBeInTheDocument();
  });

  it('shows number for pending steps', () => {
    render(<Checklist steps={steps} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('calls onStepClicked when a step is clicked', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Checklist steps={steps} onStepClicked={onClick} />);

    await user.click(screen.getByText('Process'));
    expect(onClick).toHaveBeenCalledWith('2');
  });
});
