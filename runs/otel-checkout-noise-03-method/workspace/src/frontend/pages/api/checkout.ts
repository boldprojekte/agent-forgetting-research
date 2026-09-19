// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import type { NextApiRequest, NextApiResponse } from 'next';
import InstrumentationMiddleware from '../../utils/telemetry/InstrumentationMiddleware';
import CheckoutGateway from '../../gateways/rpc/Checkout.gateway';
import { Empty, PlaceOrderRequest } from '../../protos/demo';
import { IProductCheckoutItem, IProductCheckout } from '../../types/Cart';
import ProductCatalogService from '../../services/ProductCatalog.service';
import { context, Exception, SpanStatusCode, trace } from '@opentelemetry/api';
import { SemanticAttributes } from '@opentelemetry/semantic-conventions';

type TResponse = IProductCheckout | Empty;

const GENERIC_ERROR_MESSAGE = 'Internal server error';
const PAYMENT_ERROR_MESSAGE = 'Payment failed. Please review your payment details and try again.';
const PAYMENT_FAILED_CODE = 'PAYMENT_FAILED';

/**
 * Matches payment declines surfaced by the checkout service as gRPC errors
 * originating from the payment service charge. The raw upstream message is
 * never returned to the client; it is only used to classify the failure.
 */
const isPaymentFailure = (error: unknown): boolean => {
  const message = error instanceof Error ? error.message : String(error);

  return /failed to charge card|payment request failed|charge failed|payment failed/i.test(message);
};

const recordErrorOnSpan = (error: unknown, status: number) => {
  const span = trace.getSpan(context.active());

  if (!span) return;

  span.recordException(error as Exception);
  span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
  span.setAttribute(SemanticAttributes.HTTP_STATUS_CODE, status);
};

const handler = async ({ method, body, query }: NextApiRequest, res: NextApiResponse<TResponse>) => {
  switch (method) {
    case 'POST': {
      const { currencyCode = '' } = query;
      const orderData = body as PlaceOrderRequest;

      let order;

      try {
        const placeOrderResponse = await CheckoutGateway.placeOrder(orderData);
        order = placeOrderResponse.order;
      } catch (error) {
        // Payment declines are an expected client-facing failure: return a
        // client-safe 422 without leaking upstream details. The error is still
        // recorded on the active span so telemetry keeps the failure visible.
        if (isPaymentFailure(error)) {
          recordErrorOnSpan(error, 422);

          return res.status(422).json({ error: PAYMENT_ERROR_MESSAGE, code: PAYMENT_FAILED_CODE });
        }

        // Unrelated internal failures stay generic and unclassified.
        recordErrorOnSpan(error, 500);

        return res.status(500).json({ error: GENERIC_ERROR_MESSAGE });
      }

      const { items = [], ...orderFields } = order ?? {};

      let productList: IProductCheckoutItem[];

      try {
        productList = await Promise.all(
          items.map(async ({ item: { productId = '', quantity = 0 } = {}, cost }) => {
            const product = await ProductCatalogService.getProduct(productId, currencyCode as string);

            return {
              cost,
              item: {
                productId,
                quantity,
                product,
              },
            };
          })
        );
      } catch (error) {
        // Unrelated internal failures (e.g. product catalog) stay generic and
        // unclassified, with no upstream details leaked to the client.
        recordErrorOnSpan(error, 500);

        return res.status(500).json({ error: GENERIC_ERROR_MESSAGE });
      }

      return res.status(200).json({ ...orderFields, items: productList });
    }

    default: {
      return res.status(405).send('');
    }
  }
};

export default InstrumentationMiddleware(handler);
