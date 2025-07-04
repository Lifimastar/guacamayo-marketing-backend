from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from gotrue.types import User
from gotrue.errors import AuthApiError
from stripe import APIError
from firebase_admin import messaging
from supabase import PostgrestAPIResponse
import firebase_admin
import stripe


from app.config import supabase, logger, webhook_secret
from app.models import CreatePaymentIntentRequest, NotificationRequest
from app.api.deps import get_current_user, get_current_admin_user

router = APIRouter()

# --- LÓGICA DE WEBHOOKS DE STRIPE ---
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
    
    booking_data = None
    try:
        booking_response = supabase.from_('bookings').select('id, user_id, total_price').eq('id', booking_id).maybe_single().execute()
        booking_data = booking_response.data if booking_response else None
        if booking_data:
            logger.info(f"Booking {booking_id} found.")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Database error fetching booking: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error fetching booking: {e}")
    
    if booking_data is None:
        logger.warning(f"Webhook Warning: Booking {booking_id} not found in DB for PI {stripe_payment_intent_id}. Cannot process succeeded event.")
        return True

    if 'total_price' in booking_data and booking_data['total_price'] is not None:
        try:
            db_amount = float(booking_data['total_price']) 
            if abs(db_amount - amount) > 0.01:
                logger.warning(f"Webhook Warning: Amount mismatch for booking {booking_id}. PI amount: {amount}, DB amount: {db_amount}")
        except (ValueError, TypeError):
            logger.warning(f"Webhook Warning: Could not convert booking {booking_id} total_price to float for comparison.")

    
    payment_record_data = None
    try:
        payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).maybe_single().execute()
        payment_record_data = payment_response.data if payment_response else None
        if payment_record_data:
            logger.info(f"Payment record found for booking {booking_id}.")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Database error fetching payment: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error fetching payment: {e}")

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

    else:
        logger.info(f"Creating new payment record for booking {booking_id}.")
        insert_response: PostgrestAPIResponse = supabase.from_('payments').insert({
            'booking_id': booking_id,
            'user_id': booking_data['user_id'], 
            'status': 'succeeded',
            'gateway_payment_id': stripe_payment_intent_id,
            'amount': amount,
            'currency': currency,
        }).execute()

        inserted_payment_data = None
        try:
            search_inserted_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute()
            inserted_payment_data = search_inserted_response.data 
            if inserted_payment_data is None: 
                logger.error(f"Supabase Error: Inserted payment record not found immediately after insertion for booking {booking_id}.")
                raise HTTPException(status_code=500, detail="Database error: Inserted payment record not found.")

        except APIError as e:
            if e.code == 'PGRST116' and '0 rows' in e.message:
                logger.error(f"Supabase Error: Inserted payment record not found immediately after insertion (PGRST116) for booking {booking_id}.", exc_info=True)
                raise HTTPException(status_code=500, detail="Database error: Inserted payment record not found (PGRST116).")
            else:
                logger.error(f"Supabase API Error searching for inserted payment record {booking_id}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Database error searching for inserted payment: {e.message}")

        except Exception as e:
            logger.error(f"Unexpected Error searching for inserted payment record {booking_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Unexpected error searching for inserted payment: {e}")


        payment_record_id = inserted_payment_data['id']
        logger.info(f"New payment record created for booking {booking_id} with ID: {payment_record_id}")


    logger.info(f"Updating booking {booking_id} status to 'confirmed' and linking payment {payment_record_id}.")
    booking_update_response = supabase.from_('bookings').update({
        'status': 'confirmed', 
        'payment_id': payment_record_id 
    }).eq('id', booking_id).execute() 

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

    else:
        logger.warning(f"Payment record not found for booking {booking_id} on failed event. Creating new record in failed state.")

        booking_user_id = None
        booking_response_for_user = supabase.from_('bookings').select('user_id').eq('id', booking_id).maybe_single().execute()

        booking_user_data = booking_response_for_user.data if booking_response_for_user else None 

        if booking_user_data is None or booking_user_data.get('user_id') is None: 
            logger.warning(f"Webhook Warning: Cannot find booking {booking_id} or its user_id for failed payment record.")
            return True 

        booking_user_id = booking_user_data['user_id']

        insert_response: PostgrestAPIResponse = supabase.from_('payments').insert({
            'booking_id': booking_id,
            'user_id': booking_user_id, 
            'status': 'failed',
            'gateway_payment_id': stripe_payment_intent_id,
            'amount': amount, 
            'currency': currency, 
        }).execute() 

        inserted_payment_data = None
        try:
            search_inserted_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute() 
            inserted_payment_data = search_inserted_response.data 
            if inserted_payment_data is None: 
                logger.error(f"Supabase Error: Inserted failed payment record not found immediately after insertion for booking {booking_id}.")
                raise HTTPException(status_code=500, detail="Database error: Inserted failed payment record not found.")

        except APIError as e:
            if e.code == 'PGRST116' and '0 rows' in e.message:
                logger.error(f"Supabase Error: Inserted failed payment record not found immediately after insertion (PGRST116) for booking {booking_id}.", exc_info=True)
                raise HTTPException(status_code=500, detail="Database error: Inserted failed payment record not found (PGRST116).")
            else:
                logger.error(f"Supabase API Error searching for inserted failed payment record {booking_id}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Database error searching for inserted failed payment: {e.message}") # Lanza 500

        except Exception as e:
            logger.error(f"Unexpected Error searching for inserted failed payment record {booking_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Unexpected error searching for inserted failed payment: {e}") # Lanza 500


        payment_record_id = inserted_payment_data['id']
        logger.info(f"New failed payment record created for booking {booking_id} with ID: {payment_record_id}")


    logger.info(f"Updating booking {booking_id} status to 'payment_failed' and linking payment {payment_record_id}.")
    booking_update_response = supabase.from_('bookings').update({
        'status': 'payment_failed',
        'payment_id': payment_record_id
    }).eq('id', booking_id).execute()

    logger.info(f"Successfully processed payment_intent.payment_failed for booking {booking_id}. Booking status updated to 'payment_failed'.")
    return True 

@router.post("/stripe-webhook")
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

# --- LÓGICA DE NOTIFICACIONES ---
async def _send_fcm_notification(token: str, title: str, body: str, booking_id: str):
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                'click_action': 'FLUTTER_NOTIFICATION_CLICK', 
                'booking_id': booking_id,
            },
            token=token,
        )
        response = messaging.send(message)
        logger.info(f'Notificacion enviada con exito a token {token[:10]}...: {response}')
        return True
    except firebase_admin.exceptions.UnregisteredError:
        logger.warning(f"El token FCM {token[:10]}... ya no esta registrado. Deberia ser eliminado de la DB.")
        return False
    except Exception as e:
        logger.error(f"Error al enviar notificacion a token {token:[:10]}...: {e}", exc_info=True)
        return False

@router.post("/admin/notify-user")
async def notify_user(
    request_body: NotificationRequest,
    current_admin_user: User = Depends(get_current_admin_user)
):
    logger.info(f"Admin {current_admin_user.id} intentando notificar al usuario {request_body.user_id}")

    try:
        profile_response = supabase.from_('profiles').select('fcm_token').eq('id', request_body.user_id).single().execute()

        if not profile_response.data or profile_response.data.get('fcm_token') is None:
            logger.warning(f"No se encontro fcm_token para el usuario {request_body.user_id}. No se puede enviar notificacion.")
            return JSONResponse(content={"sent": False, "reason": "FCM token not found"}, status_code=404)
        
        fcm_token = profile_response.data['fcm_token']

        success = await _send_fcm_notification(
            token=fcm_token,
            title=request_body.title,
            body=request_body.body,
            booking_id=request_body.booking_id,
        )

        if success:
            return JSONResponse(content={'sent': True}, status_code=200)
        else:
            raise HTTPException(status_code=500, detail="Failed to send notificacion via FCM")
        
    except Exception as e:
        logger.error(f'Error en el endpoint /admin/notify-user: {e}', exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))   

# --- LÓGICA DE GESTIÓN DE USUARIOS ---
@router.delete("/admin/users/{user_id}") 
async def delete_user_by_admin(user_id: str, current_admin_user: User = Depends(get_current_admin_user)):

    logger.info(f"Admin user {current_admin_user.id} attempting to delete user {user_id}")
    logger.info(f"Attempting to delete user with ID: {user_id}")

    try:
        logger.info(f"Calling supabase.auth.admin.delete_user({user_id})")
        delete_response = supabase.auth.admin.delete_user(user_id) 
        logger.info(f"Response from supabase.auth.admin.delete_user: {delete_response}")

        if delete_response is None:
            logger.info(f"User {user_id} deleted successfully by admin {current_admin_user.id}")
            return JSONResponse(content={"message": "User deleted successfully"}, status_code=200)
        else:
            logger.error(f"Unexpected response from supabase.auth.admin.delete_user for user {user_id}: {delete_response}")
            raise HTTPException(status_code=500, detail="Unexpected response from user deletion.")

    except AuthApiError as e: 
         logger.error(f"Supabase Auth Admin Error deleting user {user_id}: {e}", exc_info=True)
         if e.status == 404:
            raise HTTPException(status_code=404, detail=f"User with ID {user_id} not found in Auth.")
         elif e.status == 403:
            raise HTTPException(status_code=403, detail="Auth Admin permission denied. Check service_role_key.")
         else:
            raise HTTPException(status_code=500, detail=f"Supabase Auth Admin error: {e.message}")

    except Exception as e:
        logger.error(f"Unexpected Error deleting user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred while deleting user: {e}")
    
# --- LÓGICA DE PAGOS ---
@router.post("/create-payment-intent")
async def create_payment_intent(
    request_body: CreatePaymentIntentRequest,
    current_user: User = Depends(get_current_user) 
):
    logger.info(f"User {current_user.id} creating Payment Intent for booking {request_body.bookingId}")

    try:
        customer = stripe.Customer.create(
            metadata={'user_id': current_user.id}
        )

        ephemeral_key = stripe.EphemeralKey.create(
            customer=customer.id,
            stripe_version='2024-04-10', 
        )

        payment_intent = stripe.PaymentIntent.create(
            amount=int(request_body.amount * 100), 
            currency=request_body.currency,
            customer=customer.id,
            automatic_payment_methods={'enabled': True},
            metadata={'booking_id': request_body.bookingId, 'user_id': current_user.id}
        )


        existing_payment_response = supabase.from_('payments').select('id').eq('booking_id', request_body.bookingId).maybe_single().execute()
        existing_payment = existing_payment_response.data if existing_payment_response.data else None

        if existing_payment:

            logger.info(f"Found existing payment record for booking {request_body.bookingId}. Updating it.")
            update_response = supabase.from_('payments').update({
                'status': 'pending',
                'gateway_payment_id': payment_intent.id,
                'updated_at': 'now()'
            }).eq('booking_id', request_body.bookingId).execute()

            if not update_response.data:
                raise Exception("Failed to update existing payment record.")
        else:

            logger.info(f"No existing payment record found for booking {request_body.bookingId}. Creating a new one.")
            payment_insert_response = supabase.from_('payments').insert({
                'booking_id': request_body.bookingId,
                'user_id': current_user.id,
                'amount': request_body.amount,
                'currency': request_body.currency,
                'status': 'pending',
                'payment_gateway': 'stripe',
                'gateway_payment_id': payment_intent.id, 
            }).execute()

            if not payment_insert_response.data:
                error_message = f"Failed to create pending payment record: {payment_insert_response.error.message if payment_insert_response.error else 'Unknown database error'}"
                logger.error(error_message)
                raise Exception(error_message)


            payment_id = payment_insert_response.data[0]['id']
            supabase.from_('bookings').update({'payment_id': payment_id}).eq('id', request_body.bookingId).execute()



        return {
            "paymentIntent": payment_intent.client_secret,
            "ephemeralKey": ephemeral_key.secret,
            "customer": customer.id,
        }

    except Exception as e:
        logger.error(f"Error creating Payment Intent for booking {request_body.bookingId}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))