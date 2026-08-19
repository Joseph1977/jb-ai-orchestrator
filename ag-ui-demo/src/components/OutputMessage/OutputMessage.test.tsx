// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import OutputMessage from './OutputMessage';

describe('OutputMessage', () => {
  it('renders info message with correct role', () => {
    render(<OutputMessage type="info" body="All systems go" />);
    const el = screen.getByRole('status');
    expect(el).toHaveTextContent('All systems go');
  });

  it('renders warning message with alert role', () => {
    render(<OutputMessage type="warning" body="Disk almost full" />);
    const el = screen.getByRole('alert');
    expect(el).toHaveTextContent('Disk almost full');
  });

  it('renders optional title', () => {
    render(<OutputMessage type="info" title="Notice" body="Hello" />);
    expect(screen.getByText('Notice')).toBeInTheDocument();
  });
});
