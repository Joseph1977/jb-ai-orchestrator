// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import InputMessage from './InputMessage';

describe('InputMessage — yesno', () => {
  it('renders yes/no buttons and submits yes', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <InputMessage
        input={{ mode: 'yesno', question: 'Continue?' }}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText('Continue?')).toBeInTheDocument();

    await user.click(screen.getByText('Yes'));
    expect(onSubmit).toHaveBeenCalledWith({ answer: 'yes' });
  });

  it('submits no', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <InputMessage
        input={{ mode: 'yesno', question: 'Sure?' }}
        onSubmit={onSubmit}
      />,
    );

    await user.click(screen.getByText('No'));
    expect(onSubmit).toHaveBeenCalledWith({ answer: 'no' });
  });
});

describe('InputMessage — checkbox', () => {
  it('renders options and submits selected', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <InputMessage
        input={{
          mode: 'checkbox',
          question: 'Pick toppings',
          options: [
            { label: 'Cheese', value: 'cheese' },
            { label: 'Pepperoni', value: 'pepperoni' },
            { label: 'Mushrooms', value: 'mushrooms' },
          ],
        }}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText('Pick toppings')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Cheese'));
    await user.click(screen.getByLabelText('Mushrooms'));
    await user.click(screen.getByText('Submit'));

    expect(onSubmit).toHaveBeenCalledWith({
      selected: expect.arrayContaining(['cheese', 'mushrooms']),
    });
    expect(onSubmit.mock.calls[0][0].selected).toHaveLength(2);
  });
});
