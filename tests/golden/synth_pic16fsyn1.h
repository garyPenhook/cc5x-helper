// HEADER FILE
#pragma chip PIC16FSYN1, core 14 enh, code 2048, ram 32 : 0x7F // 96 bytes
#pragma ramdef  0x70 : 0x7F mapped_into_all_banks

#define INT_enh_style

#pragma wideConstData p

/* Predefined:
  char *FSR0, *FSR1;
  char INDF0, INDF1;
  char FSR0L, FSR0H, FSR1L, FSR1H;
  char W, WREG;
  char PCL, PCLATH, BSR, STATUS, INTCON;
  bit Carry, DC, Zero_, PD, TO;
*/

char PORTA @ 0xC;

char TRISA @ 0x8C;


bit RA0 @ PORTA.0;
bit RA1 @ PORTA.1;


#if __CC5X__ >= 3600  &&  !defined _DISABLE_DYN_CONFIG
#pragma config /1 0x3FFC FOSC = LP // LP oscillator
#pragma config /1 0x3FFF FOSC = INTOSC // internal oscillator
#pragma config /1 0x3FCF BBSIZE = _8192 // Boot Block Size (Words) 8192
#pragma config /1 0x3FFF BBSIZE = _512 // Boot Block Size (Words) 512
#endif
