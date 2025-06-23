from pydantic import BaseModel

class CreatePaymentIntentRequest(BaseModel):
    bookingId: str
    amount: float 
    currency: str 

class NotificationRequest(BaseModel):
    user_id: str
    title: str
    body: str
    booking_id: str