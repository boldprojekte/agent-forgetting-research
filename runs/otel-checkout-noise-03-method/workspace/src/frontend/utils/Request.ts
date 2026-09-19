// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

interface IRequestParams {
  url: string;
  body?: object;
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  queryParams?: Record<string, any>;
  headers?: Record<string, string>;
}

export interface IResponseErrorPayload {
  error?: string;
  code?: string;
}

/**
 * Error thrown when the server responds with a non-success HTTP status.
 * Carries the status code, a client-safe message and, when the body is JSON,
 * the structured payload (e.g. `{ error, code }`).
 */
export class RequestError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly payload?: IResponseErrorPayload;

  constructor(message: string, status: number, payload?: IResponseErrorPayload) {
    super(message);
    this.name = 'RequestError';
    this.status = status;
    this.code = payload?.code;
    this.payload = payload;
  }
}

const ERROR_MESSAGE_FALLBACK = 'Request failed';

/**
 * Safely extracts a client-safe error message and structured payload from a
 * response body. JSON bodies keep their server-provided message; non-JSON
 * bodies (HTML error pages, empty bodies, malformed JSON) fall back to a
 * generic message instead of throwing.
 */
const parseErrorBody = (status: number, responseText: string): RequestError => {
  const trimmed = responseText.trim();

  if (trimmed) {
    try {
      const parsed = JSON.parse(trimmed);

      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const payload = parsed as IResponseErrorPayload;
        const message =
          typeof payload.error === 'string' && payload.error.trim() ? payload.error : ERROR_MESSAGE_FALLBACK;

        return new RequestError(message, status, {
          error: message,
          ...(typeof payload.code === 'string' && payload.code.trim() ? { code: payload.code } : {}),
        });
      }
    } catch {
      // Non-JSON body (e.g. HTML error page): fall through to generic error.
    }
  }

  return new RequestError(ERROR_MESSAGE_FALLBACK, status, { error: ERROR_MESSAGE_FALLBACK });
};

const request = async <T>({
  url = '',
  method = 'GET',
  body,
  queryParams = {},
  headers = {
    'content-type': 'application/json',
  },
}: IRequestParams): Promise<T> => {
  const response = await fetch(`${url}?${new URLSearchParams(queryParams).toString()}`, {
    method,
    body: body ? JSON.stringify(body) : undefined,
    headers,
  });

  const responseText = await response.text();

  if (!response.ok) {
    throw parseErrorBody(response.status, responseText);
  }

  if (!!responseText) return JSON.parse(responseText);

  return undefined as unknown as T;
};

export default request;
