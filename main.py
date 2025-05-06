from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
import logging 
from postgrest.exceptions import APIError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
webhook_secret = os.getenv('STRIPE_WEBHOOK_SIGNING_SECRET')

supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
if not supabase_url or not supabase_key:
    logger.error("Supabase URL or Service Role Key not configured!")


supabase: Client = create_client(supabase_url, supabase_key)


app = FastAPI()

@app.get("/")
async def read_root():
    return {"message": "Backend is running"}


async def _handle_payment_intent_succeeded(payment_intent: dict):
    """Maneja el evento payment_intent.succeeded."""
    booking_id = payment_intent['metadata'].get('booking_id')
    stripe_payment_intent_id = payment_intent['id']
    amount = payment_intent['amount'] / 100.0
    currency = payment_intent['currency']
    user_id_from_metadata = payment_intent['metadata'].get('user_id')

    logger.info(f"Processing payment_intent.succeeded for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

    if not booking_id:
        logger.warning(f"Webhook Warning: payment_intent.succeeded event missing booking_id in metadata for PI {stripe_payment_intent_id}")
        return True


    booking_response = supabase.from_('bookings').select('id, user_id, total_price').eq('id', booking_id).maybe_single().execute()

    booking_data = booking_response.data if booking_response else None

    if booking_data is None:
        logger.warning(f"Webhook Warning: Booking {booking_id} not found in DB for PI {stripe_payment_intent_id}. Cannot process succeeded event.")
        return True

    logger.info(f"Booking {booking_id} found.")

    if 'total_price' in booking_data and booking_data['total_price'] is not None:
        try:
            db_amount = float(booking_data['total_price']) 
            if abs(db_amount - amount) > 0.01:
                logger.warning(f"Webhook Warning: Amount mismatch for booking {booking_id}. PI amount: {amount}, DB amount: {db_amount}")
        except (ValueError, TypeError):
            logger.warning(f"Webhook Warning: Could not convert booking {booking_id} total_price to float for comparison.")

    
    payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).maybe_single().execute()

    payment_record_data = payment_response.data if payment_response else None

    payment_record_id = None

    if payment_record_data:
        payment_record_id = payment_record_data['id']
        logger.info(f"Updating payment record {payment_record_id} status to 'succeeded'.")
        update_response = supabase.from_('payments').update({
            'status': 'succeeded',
            'gateway_payment_id': stripe_payment_intent_id,
            'amount': amount,
            'currency': currency,
            'updated_at': 'now()'
        }).eq('id', payment_record_id).execute() 

        if update_response.error:
            logger.error(f"Supabase Error: Failed to update payment record {payment_record_id} for booking {booking_id} - {update_response.error}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error updating payment: {update_response.error.message}")

    else:
        logger.info(f"Creating new payment record for booking {booking_id}.")
        insert_response = supabase.from_('payments').insert({
            'booking_id': booking_id,
            'user_id': booking_data['user_id'], 
            'status': 'succeeded',
            'gateway_payment_id': stripe_payment_intent_id,
            'amount': amount,
            'currency': currency,
        }).select('id').single().execute()

        inserted_payment_data = insert_response.data

        if insert_response.error: 
            logger.error(f"Supabase Error: Failed to insert new payment record for booking {booking_id} - {insert_response.error}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error inserting payment: {insert_response.error.message}")

        if inserted_payment_data is None:
            logger.error(f"Supabase Error: Inserted payment data is None for booking {booking_id} after successful insert execution.")
            raise HTTPException(status_code=500, detail="Database error inserting payment: Inserted data is null.")


        payment_record_id = inserted_payment_data['id']
        logger.info(f"New payment record created for booking {booking_id} with ID: {payment_record_id}")


    logger.info(f"Updating booking {booking_id} status to 'confirmed' and linking payment {payment_record_id}.")
    booking_update_response = supabase.from_('bookings').update({
        'status': 'confirmed', 
        'payment_id': payment_record_id 
    }).eq('id', booking_id).execute() 

    if booking_update_response.error:
        logger.error(f"Supabase Error: Failed to update booking status for {booking_id} - {booking_update_response.error}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error updating booking status: {booking_update_response.error.message}")

    logger.info(f"Successfully processed payment_intent.succeeded for booking {booking_id}. Booking status updated to 'confirmed'.")
    return True 


async def _handle_payment_intent_failed(payment_intent: dict):
    """Maneja el evento payment_intent.payment_failed."""
    booking_id = payment_intent['metadata'].get('booking_id')
    stripe_payment_intent_id = payment_intent['id']
    user_id_from_metadata = payment_intent['metadata'].get('user_id')
    amount = payment_intent.get('amount') / 100.0 if payment_intent.get('amount') is not None else 0.0
    currency = payment_intent.get('currency') if payment_intent.get('currency') is not None else 'usd'

    logger.warning(f"Processing payment_intent.payment_failed for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

    if not booking_id:
        logger.warning(f"Webhook Warning: payment_intent.payment_failed event missing booking_id in metadata for PI {stripe_payment_intent_id}")
        return True 
    
    payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).maybe_single().execute()

    payment_record_data = payment_response.data if payment_response else None 

    payment_record_id = None

    if payment_record_data:
        payment_record_id = payment_record_data['id']
        logger.info(f"Updating payment record {payment_record_id} status to 'failed'.")
        update_response = supabase.from_('payments').update({
            'status': 'failed',
            'gateway_payment_id': stripe_payment_intent_id,
            'updated_at': 'now()'
        }).eq('id', payment_record_id).execute()

        if update_response.error:
            logger.error(f"Supabase Error: Failed to update payment record {payment_record_id} to failed for booking {booking_id} - {update_response.error}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error updating payment to failed: {update_response.error.message}")

    else:
        logger.warning(f"Payment record not found for booking {booking_id} on failed event. Creating new record in failed state.")

        booking_response_for_user = supabase.from_('bookings').select('user_id').eq('id', booking_id).maybe_single().execute()

        booking_user_id = booking_response_for_user.data['user_id'] if booking_response_for_user and booking_response_for_user.data else None

        if not booking_user_id:
            logger.warning(f"Webhook Warning: Cannot find user_id for booking {booking_id} on failed event. Cannot create payment record.")
            return True

        insert_response = supabase.from_('payments').insert({
            'booking_id': booking_id,
            'user_id': booking_user_id, 
            'status': 'failed',
            'gateway_payment_id': stripe_payment_intent_id,
            'amount': amount, 
            'currency': currency, 
        }).select('id').single().execute() 

        inserted_payment_data = insert_response.data 

        if insert_response.error: 
              logger.error(f"Supabase Error: Failed to insert new payment record for booking {booking_id} - {insert_response.error}", exc_info=True)
              raise HTTPException(status_code=500, detail=f"Database error inserting payment: {insert_response.error.message}")

        if inserted_payment_data is None: 
              logger.error(f"Supabase Error: Inserted payment data is None for booking {booking_id} after successful insert execution.")
              raise HTTPException(status_code=500, detail="Database error inserting payment: Inserted data is null.")

        payment_record_id = inserted_payment_data['id']
        logger.info(f"New payment record created for booking {booking_id} with ID: {payment_record_id}")


    logger.info(f"Updating booking {booking_id} status to 'payment_failed' and linking payment {payment_record_id}.")
    booking_update_response = supabase.from_('bookings').update({
        'status': 'payment_failed',
        'payment_id': payment_record_id
    }).eq('id', booking_id).execute()

    if booking_update_response.error:
        logger.error(f"Supabase Error: Failed to update booking status to failed for {booking_id} - {booking_update_response.error}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error updating booking status to failed: {booking_update_response.error.message}")


    logger.info(f"Successfully processed payment_intent.payment_failed for booking {booking_id}. Booking status updated to 'payment_failed'.")
    return True 

# --- Endpoint Principal del Webhook ---

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('Stripe-Signature')

    if not webhook_secret:
        logger.error("Webhook signing secret not configured!")
        raise HTTPException(status_code=500, detail="Webhook signing secret not configured.")

    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError as e:
        logger.error(f"Webhook Error: Invalid payload - {e}")
        return JSONResponse(content={"detail": "Invalid payload"}, status_code=400)

    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Webhook Error: Invalid signature - {e}")
        return JSONResponse(content={"detail": "Invalid signature"}, status_code=400)

    except Exception as e:
        logger.error(f"Webhook Error: Unhandled verification error - {e}")
        return JSONResponse(content={"detail": "Webhook signature verification failed."}, status_code=400)

    event_type = event['type']
    event_data = event['data']
    event_object = event_data['object']

    logger.info(f"Received Stripe event: {event_type}")

    # --- Lógica de Enrutamiento de Eventos ---
    processed = False 

    try:
        if event_type == 'payment_intent.succeeded':
            processed = await _handle_payment_intent_succeeded(event_object)
        elif event_type == 'payment_intent.payment_failed':
            processed = await _handle_payment_intent_failed(event_object)

        else:
            logger.info(f"Unhandled event type: {event_type}. Returning 200 OK.")
            processed = True

    except HTTPException as e:
        logger.error(f"Webhook Handler HTTPException: {e.detail}", exc_info=True)
        raise e 

    except Exception as e:
        logger.error(f"Webhook Handler Unexpected Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An unexpected error occurred during webhook processing.") 

    return JSONResponse(content={"received": True, "event_type": event_type, "processed": processed}, status_code=200)