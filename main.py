from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
from dotenv import load_dotenv 
from supabase import create_client, Client
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
webhook_secret = os.getenv('STRIPE_WEBHOOK_SIGNING_SECRET') 

supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
if not supabase_url or not supabase_key:
    logger.error('Supabase URL or Service Role Key not configured!')

supabase: Client = create_client(supabase_url, supabase_key)


app = FastAPI()

@app.get("/")
async def read_root():
    return {"message": "Backend is running"}

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

    try:
        if event_type == 'payment_intent.succeeded':
            payment_intent = event_object
            booking_id = payment_intent['metadata'].get('booking_id')
            stripe_payment_intent_id = payment_intent['id']
            amount = payment_intent['amount'] / 100.0 
            currency = payment_intent['currency']
            user_id_from_metadata = payment_intent['metadata'].get('user_id')


            logger.info(f"Processing payment_intent.succeeded for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

            if not booking_id:
                 logger.warning(f"Webhook Warning: payment_intent.succeeded event missing booking_id in metadata for PI {stripe_payment_intent_id}")
                 return JSONResponse(content={"received": True, "message": "Missing booking_id metadata"}, status_code=200)
            
            try:
                booking_response = supabase.from_('bookings').select('id, user_id, total_price').eq('id', booking_id).single().execute()
                booking_data = booking_response.data
            
            except Exception as db_fetch_error:
                logger.error(f'Supabase Error: Failed to fetch booking {booking_id} for PI {stripe_payment_intent_id} - {db_fetch_error}')
            
                if hasattr(db_fetch_error, 'code') and db_fetch_error.code == 406:
                    logger.warning(f'Webhook Warning: Booking {booking_id} not found in DB for PI {stripe_payment_intent_id}')
                    return JSONResponse(content={'received': True, 'message': 'Booking not found in DB'}, status_code=200)
                else:
                    raise HTTPException(status_code=500, detail=f'Database error fetching booking: {db_fetch_error}')
            
            if not booking_data:
                logger.warning(f'Webhook Warning: Booking {booking_id} data is empty after fetch for PI {stripe_payment_intent_id}')
                return JSONResponse(content={'received': True, 'message': 'Booking data is empty'}, status_code=200)

            if abs(booking_data['total_price'] - amount) > 0.01: 
                logger.warning(f"Webhook Warning: Amount mismatch for booking {booking_id}. PI amount: {amount}, DB amount: {booking_data['total_price']}")

            try:
                payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute()
                payment_record_id = payment_response.data['id']
                logger.info(f'Payment record found for booking {booking_id}: {payment_record_id}. Updating status.')

                update_response = supabase.from_('payments').update({
                    'status': 'succeeded',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': amount,
                    'currency': currency,
                    'updated_at': 'now()'
                }).eq('id', payment_record_id).execute()

            except Exception as db_fetch_or_update_error:
                logger.warning(f'Payment record not found for booking {booking_id} on succeeded or faield update. Creating new record.')
                insert_response = supabase.from_('payments').insert({
                    'booking_id': booking_id,
                    'user_id': booking_data['user_id'],
                    'status': 'succeeded',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': amount,
                    'currency': currency,
                }).select('id').single().execute()

                if insert_response.error:
                    logger.error(f"Supabase Error: Failed to insert new payment record for booking {booking_id} - {insert_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error inserting payment: {insert_response.error.message}")

                payment_record_id = insert_response.data['id']


            booking_update_response = supabase.from_('bookings').update({
                'status': 'confirmed', 
                'payment_id': payment_record_id 
            }).eq('id', booking_id).execute()

            if booking_update_response.error:
                 logger.error(f"Supabase Error: Failed to update booking status for {booking_id} - {booking_update_response.error}")
                 raise HTTPException(status_code=500, detail=f"Database error updating booking status: {booking_update_response.error.message}")


            logger.info(f"Successfully processed payment_intent.succeeded for booking {booking_id}. Booking status updated to 'confirmed'.")
            return JSONResponse(content={"received": True, "status": "booking_confirmed"}, status_code=200)


        elif event_type == 'payment_intent.payment_failed':
            payment_intent = event_object
            booking_id = payment_intent['metadata'].get('booking_id')
            stripe_payment_intent_id = payment_intent['id']
            user_id_from_metadata = payment_intent['metadata'].get('user_id') 

            logger.warning(f"Processing payment_intent.payment_failed for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

            if not booking_id:
                logger.warning(f"Webhook Warning: payment_intent.payment_failed event missing booking_id in metadata for PI {stripe_payment_intent_id}")
                return JSONResponse(content={"received": True, "message": "Missing booking_id metadata"}, status_code=200)

            try:

                payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute()
                payment_record_id = payment_response.data['id']
                logger.info(f"Payment record found for booking {booking_id}: {payment_record_id}. Updating status to failed.")

                update_response = supabase.from_('payments').update({
                    'status': 'failed',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'updated_at': 'now()'
                }).eq('id', payment_record_id).execute()

                if update_response.error:
                    logger.error(f"Supabase Error: Failed to update payment record {payment_record_id} to failed for booking {booking_id} - {update_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error updating payment to failed: {update_response.error.message}")

            except Exception as db_fetch_or_update_error:

                logger.warning(f"Payment record not found for booking {booking_id} on failed event. Creating new record in failed state.")
                booking_response = supabase.from_('bookings').select('user_id').eq('id', booking_id).single().execute()
                booking_user_id = booking_response.data['user_id'] if booking_response.data else None

                if not booking_user_id:
                    logger.warning(f"Webhook Warning: Cannot find user_id for booking {booking_id} on failed event.")
                    return JSONResponse(content={"received": True, "message": "Booking user_id not found"}, status_code=200)


                insert_response = supabase.from_('payments').insert({
                    'booking_id': booking_id,
                    'user_id': booking_user_id, 
                    'status': 'failed',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': payment_intent.get('amount') / 100.0,
                    'currency': payment_intent.get('currency'),
                }).select('id').single().execute() 

                if insert_response.error:
                    logger.error(f"Supabase Error: Failed to insert new failed payment record for booking {booking_id} - {insert_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error inserting failed payment: {insert_response.error.message}")

                payment_record_id = insert_response.data['id'] 


            booking_update_response = supabase.from_('bookings').update({
                'status': 'payment_failed',
                'payment_id': payment_record_id
            }).eq('id', booking_id).execute()

            if booking_update_response.error:
                logger.error(f"Supabase Error: Failed to update booking status to failed for {booking_id} - {booking_update_response.error}")
                raise HTTPException(status_code=500, detail=f"Database error updating booking status to failed: {booking_update_response.error.message}")


            logger.info(f"Successfully processed payment_intent.payment_failed for booking {booking_id}. Booking status updated to 'payment_failed'.")
            return JSONResponse(content={"received": True, "status": "booking_payment_failed"}, status_code=200)

        else:
            logger.info(f"Unhandled event type: {event_type}")
            return JSONResponse(content={"received": True, "message": "Event type not handled"}, status_code=200)

    except HTTPException as e:
         logger.error(f"Webhook Processing HTTPException: {e.detail}", exc_info=True) 
         raise e

    except Exception as e:
        logger.error(f"Webhook Processing Unexpected Error: {e}", exc_info=True) 
        raise HTTPException(status_code=500, detail="An unexpected error occurred during webhook processing.")