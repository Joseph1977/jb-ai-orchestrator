// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import ApprovalPrompt from './ApprovalPrompt';

describe('ApprovalPrompt', () => {
  it('renders title and description', () => {
    render(
      <ApprovalPrompt
        title="Deploy v2.0"
        description="This will update production"
        onDecision={vi.fn()}
      />,
    );
    expect(screen.getByText('Deploy v2.0')).toBeInTheDocument();
    expect(screen.getByText('This will update production')).toBeInTheDocument();
  });

  it('calls onDecision with approved=true on Approve', async () => {
    const user = userEvent.setup();
    const onDecision = vi.fn();
    render(
      <ApprovalPrompt title="Deploy" onDecision={onDecision} />,
    );

    await user.click(screen.getByText('Approve'));
    expect(onDecision).toHaveBeenCalledWith({ approved: true });
  });

  it('shows reason textarea on first Decline, then submits on second', async () => {
    const user = userEvent.setup();
    const onDecision = vi.fn();
    render(
      <ApprovalPrompt title="Deploy" onDecision={onDecision} />,
    );

    // first click reveals textarea
    await user.click(screen.getByText('Decline'));
    expect(onDecision).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText(/reason/i)).toBeInTheDocument();

    // type reason and confirm
    await user.type(screen.getByPlaceholderText(/reason/i), 'Not ready');
    await user.click(screen.getByText('Confirm Decline'));
    expect(onDecision).toHaveBeenCalledWith({ approved: false, reason: 'Not ready' });
  });
});
