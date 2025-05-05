# main.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
from dotenv import load_dotenv
from supabase import create_client, Client
import json
import logging # Importa el módulo de logging

# Configura logging básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
webhook_secret = os.getenv('STRIPE_WEBHOOK_SIGNING_SECRET')

supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
# Asegúrate de que las claves de Supabase estén configuradas
if not supabase_url or not supabase_key:
    logger.error("Supabase URL or Service Role Key not configured!")
    # En un backend real, podrías querer que la app no inicie si esto falla
    # Por ahora, solo loggeamos el error. Las operaciones de DB fallarán.

supabase: Client = create_client(supabase_url, supabase_key)


app = FastAPI()

@app.get("/")
async def read_root():
    return {"message": "Backend is running"}

# Endpoint para webhooks de Stripe
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


    # Si la verificación fue exitosa, procesa el evento
    event_type = event['type']
    event_data = event['data']
    event_object = event_data['object']

    logger.info(f"Received Stripe event: {event_type}")
    # logger.info(json.dumps(event_object, indent=2)) # Opcional: imprimir el objeto completo

    # --- Lógica de Procesamiento de Eventos ---
    try:
        if event_type == 'payment_intent.succeeded':
            payment_intent = event_object
            booking_id = payment_intent['metadata'].get('booking_id') # Obtén el booking_id de metadata
            stripe_payment_intent_id = payment_intent['id']
            amount = payment_intent['amount'] / 100.0 # Convertir de centavos a unidad principal (usar 100.0 para float division)
            currency = payment_intent['currency']
             # Obtén el user_id de metadata si lo enviaste
            user_id_from_metadata = payment_intent['metadata'].get('user_id')


            logger.info(f"Processing payment_intent.succeeded for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

            if not booking_id:
                logger.warning(f"Webhook Warning: payment_intent.succeeded event missing booking_id in metadata for PI {stripe_payment_intent_id}")
                 # Aunque falta metadata, el webhook es válido. Devuelve 200.
                return JSONResponse(content={"received": True, "message": "Missing booking_id metadata"}, status_code=200) # Devuelve 200 para no reintentar

            # 1. Busca la reserva en Supabase para verificarla y obtener user_id si no vino en metadata
            # Usamos el cliente Supabase con service_role_key
            booking_response = supabase.from_('bookings').select('id, user_id, total_price').eq('id', booking_id).single().execute()

            if booking_response.error:
                logger.error(f"Supabase Error: Failed to fetch booking {booking_id} for PI {stripe_payment_intent_id} - {booking_response.error}")
                # Lanza una excepción para que Stripe reintente (problema temporal de DB)
                raise HTTPException(status_code=500, detail=f"Database error fetching booking: {booking_response.error.message}")

            booking_data = booking_response.data

            if not booking_data:
                logger.warning(f"Webhook Warning: Booking {booking_id} not found in DB for PI {stripe_payment_intent_id}")
                # La reserva no existe, algo va mal. Devuelve 200 para no reintentar.
                return JSONResponse(content={"received": True, "message": "Booking not found in DB"}, status_code=200)

             # Opcional: Verificar que el monto pagado coincide con el de la reserva
             # if abs(booking_data['total_price'] - amount) > 0.01: # Permitir pequeña diferencia por decimales
             #     logger.warning(f"Webhook Warning: Amount mismatch for booking {booking_id}. PI amount: {amount}, DB amount: {booking_data['total_price']}")
             #     # Decide si esto es un error crítico. Podrías loggearlo y proceder, o devolver 400/500.
             #     # Para empezar, solo loggeamos.


            # 2. Busca o crea el registro de pago en la tabla 'payments'
            # Es mejor buscar por booking_id ya que configuramos UNIQUE en payments.booking_id
            payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute()

            payment_record_id = None

            if payment_response.data:
                 # Si el registro de pago ya existe, obtenemos su ID
                payment_record_id = payment_response.data['id']
                logger.info(f"Payment record found for booking {booking_id}: {payment_record_id}. Updating status.")
                 # Actualiza el registro de pago existente
                update_response = supabase.from_('payments').update({
                    'status': 'succeeded',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': amount,
                    'currency': currency,
                    'updated_at': 'now()'
                }).eq('id', payment_record_id).execute()

                if update_response.error:
                    logger.error(f"Supabase Error: Failed to update payment record {payment_record_id} for booking {booking_id} - {update_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error updating payment: {update_response.error.message}")

            else:
                 # Si el registro de pago NO existe (esto podría pasar si no lo creaste en el endpoint create-payment-intent)
                 # Lo creamos ahora. Usamos el user_id de la reserva.
                logger.info(f"Payment record not found for booking {booking_id}. Creating new record.")
                insert_response = supabase.from_('payments').insert({
                    'booking_id': booking_id,
                    'user_id': booking_data['user_id'], # Usa el user_id de la reserva encontrada
                    'status': 'succeeded',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': amount,
                    'currency': currency,
                     # created_at se establecerá por defecto
                }).select('id').single().execute() # Selecciona el ID del registro insertado

                if insert_response.error:
                    logger.error(f"Supabase Error: Failed to insert new payment record for booking {booking_id} - {insert_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error inserting payment: {insert_response.error.message}")

                payment_record_id = insert_response.data['id'] # Obtén el ID del registro recién creado


            # 3. Actualiza el estado de la reserva en la tabla 'bookings'
            # Vincula la reserva al registro de pago recién actualizado/creado.
            booking_update_response = supabase.from_('bookings').update({
                'status': 'confirmed', # O 'in_progress'
                'payment_id': payment_record_id # Vincula la reserva al pago
            }).eq('id', booking_id).execute()

            if booking_update_response.error:
                logger.error(f"Supabase Error: Failed to update booking status for {booking_id} - {booking_update_response.error}")
                 # Esto es crítico. La reserva no se marcó como pagada.
                 # Podrías querer enviar una alerta o tener un proceso de conciliación.
                 # Lanzamos excepción para que Stripe reintente (si es un error temporal de DB)
                raise HTTPException(status_code=500, detail=f"Database error updating booking status: {booking_update_response.error.message}")


            logger.info(f"Successfully processed payment_intent.succeeded for booking {booking_id}. Booking status updated to 'confirmed'.")
            return JSONResponse(content={"received": True, "status": "booking_confirmed"}, status_code=200) # ¡Importante! Devuelve 200 OK


        elif event_type == 'payment_intent.payment_failed':
            payment_intent = event_object
            booking_id = payment_intent['metadata'].get('booking_id')
            stripe_payment_intent_id = payment_intent['id']
            user_id_from_metadata = payment_intent['metadata'].get('user_id') # Obtén user_id si lo enviaste

            logger.warning(f"Processing payment_intent.payment_failed for PI: {stripe_payment_intent_id}, Booking ID: {booking_id}")

            if not booking_id:
                logger.warning(f"Webhook Warning: payment_intent.payment_failed event missing booking_id in metadata for PI {stripe_payment_intent_id}")
                return JSONResponse(content={"received": True, "message": "Missing booking_id metadata"}, status_code=200)


            # 1. Busca el registro de pago (o créalo si no existe)
            payment_response = supabase.from_('payments').select('id').eq('booking_id', booking_id).single().execute()

            payment_record_id = None

            if payment_response.data:
                payment_record_id = payment_response.data['id']
                logger.info(f"Payment record found for booking {booking_id}: {payment_record_id}. Updating status to failed.")
                 # Actualiza el registro de pago existente a 'failed'
                update_response = supabase.from_('payments').update({
                    'status': 'failed',
                    'gateway_payment_id': stripe_payment_intent_id, # Guarda el PI ID
                    'updated_at': 'now()'
                }).eq('id', payment_record_id).execute()

                if update_response.error:
                    logger.error(f"Supabase Error: Failed to update payment record {payment_record_id} to failed for booking {booking_id} - {update_response.error}")
                    raise HTTPException(status_code=500, detail=f"Database error updating payment to failed: {update_response.error.message}")

            else:
                 # Si el registro de pago NO existe, lo creamos ahora en estado 'failed'
                logger.warning(f"Payment record not found for booking {booking_id} on failed event. Creating new record in failed state.")
                  # Busca la reserva para obtener el user_id
                booking_response = supabase.from_('bookings').select('user_id').eq('id', booking_id).single().execute()
                booking_user_id = booking_response.data['user_id'] if booking_response.data else None

                if not booking_user_id:
                    logger.warning(f"Webhook Warning: Cannot find user_id for booking {booking_id} on failed event.")
                      # Decide qué hacer si no puedes encontrar el user_id de la reserva.
                      # Podrías insertar el pago con user_id = null o lanzar un error.
                      # Por ahora, loggeamos y devolvemos 200.
                    return JSONResponse(content={"received": True, "message": "Booking user_id not found"}, status_code=200)


                insert_response = supabase.from_('payments').insert({
                    'booking_id': booking_id,
                    'user_id': booking_user_id, # Usa el user_id de la reserva
                    'status': 'failed',
                    'gateway_payment_id': stripe_payment_intent_id,
                    'amount': payment_intent.get('amount') / 100.0, # Usa el monto del PI si está disponible
                    'currency': payment_intent.get('currency'),
                }).select('id').single().execute() # Selecciona el ID del registro insertado

                if insert_response.error:
                      logger.error(f"Supabase Error: Failed to insert new failed payment record for booking {booking_id} - {insert_response.error}")
                      raise HTTPException(status_code=500, detail=f"Database error inserting failed payment: {insert_response.error.message}")

                payment_record_id = insert_response.data['id'] # Obtén el ID del registro recién creado


            # 2. Actualiza el estado de la reserva a 'cancelled' o 'payment_failed'
            booking_update_response = supabase.from_('bookings').update({
                'status': 'payment_failed',
                'payment_id': payment_record_id # Vincula la reserva al pago fallido
            }).eq('id', booking_id).execute()

            if booking_update_response.error:
                logger.error(f"Supabase Error: Failed to update booking status to failed for {booking_id} - {booking_update_response.error}")
                raise HTTPException(status_code=500, detail=f"Database error updating booking status to failed: {booking_update_response.error.message}")


            logger.info(f"Successfully processed payment_intent.payment_failed for booking {booking_id}. Booking status updated to 'payment_failed'.")
            return JSONResponse(content={"received": True, "status": "booking_payment_failed"}, status_code=200) # ¡Importante! Devuelve 200 OK


        # --- Manejar otros eventos si es necesario ---
        # elif event_type == 'charge.refunded':
        #     # Lógica para reembolsos
        #     pass # Implementar lógica de reembolso

        else:
            # Tipo de evento no manejado, devuelve 200 OK para que Stripe no reintente
            logger.info(f"Unhandled event type: {event_type}")
            return JSONResponse(content={"received": True, "message": "Event type not handled"}, status_code=200)

    except HTTPException as e:
         # Si lanzamos HTTPException dentro del try, la manejamos aquí para loggear y devolver el status code
        logger.error(f"Webhook Processing HTTPException: {e.detail}", exc_info=True) # Loggea el traceback
        raise e # Relanza la excepción para que FastAPI la maneje y devuelva el status code

    except Exception as e:
        # Si ocurre cualquier otro error inesperado en tu lógica, loggea y devuelve 500
        logger.error(f"Webhook Processing Unexpected Error: {e}", exc_info=True) # Loggea el traceback completo
        raise HTTPException(status_code=500, detail="An unexpected error occurred during webhook processing.") # Devuelve 500 para que Stripe reintente