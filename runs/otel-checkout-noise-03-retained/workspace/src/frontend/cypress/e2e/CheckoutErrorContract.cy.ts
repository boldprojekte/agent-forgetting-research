// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import { getElementByField } from '../../utils/Cypress';
import { CypressFields } from '../../utils/enums/CypressFields';

describe('Checkout error contract', () => {
  // The frontend surfaces API failures as promise rejections in CartDetail;
  // these tests assert the HTTP/error contract of the API and request helper.
  beforeEach(() => {
    cy.on('uncaught:exception', err => {
      expect(err.message).to.not.contain('is not valid JSON');
      return false;
    });

    cy.intercept('POST', '/api/cart*').as('addToCart');
    cy.intercept('GET', '/api/cart*').as('getCart');
  });

  const addItemToCart = (position: 'first' | 'last') => {
    cy.visit('/');

    getElementByField(CypressFields.ProductCard)
      [position]()
      .click();
    getElementByField(CypressFields.ProductAddToCart).click();

    cy.wait('@addToCart');
    cy.wait('@getCart', { timeout: 10000 });
    cy.wait(2000);

    cy.location('href').should('match', /\/cart$/);
    getElementByField(CypressFields.CartItemCount).should('contain', '1');

    getElementByField(CypressFields.CartIcon).click({ force: true });
    getElementByField(CypressFields.CartGoToShopping).click();
    cy.location('href').should('match', /\/cart$/);
  };

  it('returns HTTP 422 with code PAYMENT_FAILED when the payment is declined', () => {
    addItemToCart('first');

    cy.intercept('POST', '/api/checkout*').as('placeOrder');
    getElementByField(CypressFields.CheckoutPlaceOrder).click();

    cy.wait('@placeOrder').then(interception => {
      expect(interception.response?.statusCode, 'payment decline status').to.eq(422);
      const body = interception.response?.body as { error?: string; code?: string };
      expect(body.error, 'client-safe error message').to.be.a('string').and.not.be.empty;
      expect(body.error).to.not.match(/grpc|Exception|stack|token/i);
      expect(body.code, 'error code').to.eq('PAYMENT_FAILED');
    });

    // The failure is surfaced instead of an unhandled generic 500.
    cy.location('href').should('match', /\/cart$/);
  });

  it('returns a generic HTTP 500 JSON error for non-payment internal failures', () => {
    cy.request({
      method: 'POST',
      url: '/api/checkout?currencyCode=USD',
      // Malformed order payload fails inside the checkout service before any payment.
      body: {},
      failOnStatusCode: false,
    }).then(response => {
      expect(response.status, 'internal failure status').to.eq(500);
      expect(response.body, 'generic error body').to.deep.eq({ error: 'Internal server error' });
      expect(JSON.stringify(response.body)).to.not.contain('PAYMENT_FAILED');
      expect(JSON.stringify(response.body)).to.not.match(/grpc|Exception/i);
    });
  });

  it('shared request helper handles non-JSON error bodies safely (no JSON.parse crash)', () => {
    addItemToCart('first');

    cy.intercept('POST', '/api/checkout*', {
      statusCode: 502,
      body: 'Bad Gateway',
    }).as('nonJsonError');

    getElementByField(CypressFields.CheckoutPlaceOrder).click();
    cy.wait('@nonJsonError');
    cy.wait(1000);
    cy.location('href').should('match', /\/cart$/);
  });
});

export {};
