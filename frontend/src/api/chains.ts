import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  ChainResponse,
  CreateChainRequest,
  EditChainRequest,
} from "./types";

/**
 * Flow_Controller admin calls (Requirements 10.1, 10.4, 17.3): list fallback
 * chains, read a single chain, create a chain with its ordered entries, edit a
 * chain (name and/or ordered entries), and delete a chain.
 *
 * Every function is typed against the secret-free chain schemas in
 * `gozar/api/schemas.py`; chains carry no credential material, so no `any` and no
 * secret ever crosses the API boundary. The ordered `account_ids` are the routing
 * attempt order (position 0 first, Requirement 10.1); the backend mirrors them back
 * as positioned {@link ChainResponse} entries.
 */

/** Return every Fallback_Chain with its ordered entries, ordered by the backend. */
export function listChains(): Promise<ReadonlyArray<ChainResponse>> {
  return api.get<ReadonlyArray<ChainResponse>>(ENDPOINTS.chains.list);
}

/** Read a single chain by id, or reject with a 404 ApiError when absent. */
export function getChain(chainId: string): Promise<ChainResponse> {
  return api.get<ChainResponse>(ENDPOINTS.chains.detail(chainId));
}

/** Create a chain with an ordered list of credential ids (Requirement 10.1). */
export function createChain(payload: CreateChainRequest): Promise<ChainResponse> {
  return api.post<ChainResponse>(ENDPOINTS.chains.create, payload);
}

/**
 * Edit a chain's name and/or ordered entries (Requirement 10.4).
 *
 * The backend changes only the fields present in the body (it inspects
 * `model_fields_set`), so callers should send just the fields they intend to
 * change. Uses HTTP PUT, matching `edit_chain_route` in `gozar/api/chains.py`.
 */
export function editChain(
  chainId: string,
  payload: EditChainRequest,
): Promise<ChainResponse> {
  return api.put<ChainResponse>(ENDPOINTS.chains.detail(chainId), payload);
}

/** Delete a chain and its entries (Requirement 10.4). */
export function deleteChain(chainId: string): Promise<void> {
  return api.delete<void>(ENDPOINTS.chains.detail(chainId));
}
